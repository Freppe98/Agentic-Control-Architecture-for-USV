from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import requests
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo
import asyncio
import json
import math
import os
import time
import uuid

import mission_contract
import mission_full_refresh
import mission_lifecycle
import mission_publish
import mission_reconcile
import planning
import fleet_planning
import replan_package
import scout_mission_execution
import scout_replan
import vehicle_registry
import vehicle_telemetry


@asynccontextmanager
async def lifespan(app):
    # WHICH PROCESS, AND WHICH STORE. On Windows a second `run_operator_backend.ps1` fails to
    # bind with WinError 10048 while the FIRST backend keeps serving 8210 — with its own,
    # possibly older, in-memory active mission. Nothing in the UI could tell the two apart, so
    # the station now states its identity at startup and exposes it on GET /api/diagnostics.
    # This is instrumentation, not a fix for the store: the store was never the defect.
    print(f"[OPERATOR BACKEND] pid={os.getpid()} started_at={PROCESS_STARTED_AT} "
          f"store={MISSION_STORE_PATH}")
    # Restore the durable mission store BEFORE serving: readiness, the planning-package sync
    # and the mission-execution routes all read it, and an empty store must be an honest
    # "no approved mission" rather than a race against startup. Fails closed (see
    # _load_mission_store): a corrupt snapshot starts an EMPTY store, never a partial one.
    print(f"[MISSION STORE] {_load_mission_store()}")
    # Background monitor: log comms-state transitions once per second.
    task = asyncio.create_task(_comms_monitor_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
BASE_DIR = Path(__file__).resolve().parent

# This process's identity, fixed at import. Reported at startup and on GET /api/diagnostics so
# an operator (or a script) can tell WHICH backend answered — see the lifespan note above.
PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat()

STALE_AFTER_SECONDS = 8
PARTITIONED_AFTER_SECONDS = 15
DISCONNECTED_AFTER_SECONDS = 30

# --- Canonical vehicle identity (see vehicle_registry.py) --------------------------
# ONE explicit id policy for the whole station: every per-USV store, command, mission
# cache, URL and selection is keyed by the canonical id this registry returns, and the
# display name ("Scout", "SAR-001") is a separate field that is never an identity key.
REGISTRY = vehicle_registry.load_registry()


def canonical_id(raw):
    """THE canonical-vehicle-id function (int, slug string, or None). Use it everywhere
    a vehicle identity enters the backend — packet, URL, query or request body. Returns
    None for a value that names no vehicle; callers must NOT substitute a default
    vehicle, because that is exactly how SAR's packets used to land on Scout's record."""
    return REGISTRY.canonical_id(raw)


def vehicle_slug(cid) -> str:
    """Stable string form of a canonical id ("usv-2") for URLs, log lines and payloads."""
    return REGISTRY.slug(cid)


# --- Per-USV authoritative current state -------------------------------------------
# THE fix for multi-USV state isolation. There is deliberately no single "latest status"
# object any more: whichever vehicle posted most recently used to become the only fully
# populated row in GET /api/fleet/status, so two live USVs made every page alternate
# between one complete vehicle and one UNKNOWN placeholder every couple of seconds.
#
# Every USV now owns one independent record keyed by its canonical id. A packet from
# vehicle A updates exactly A's entry — never B's telemetry, name, health, mission,
# authority, freshness or the operator's selection. The fleet endpoint is assembled from
# ALL records every time, so a vehicle that did not report this poll simply keeps its own
# last-known values and ages on its OWN clock.
#
#   current_vehicle_state = { canonical_id: {
#       "canonical_id":      2,                      # identity key (int or slug string)
#       "slug":              "usv-2",                # stable string form
#       "display_name":      "Scout",                # per-USV, stable, never an id
#       "raw_latest":        {...},                  # last ACCEPTED envelope, verbatim
#       "received_at":       datetime,               # per-USV arrival time (freshness)
#       "message_timestamp": 1712345678.9,           # per-USV monotonic guard
#       "last_known_telemetry": {...},               # same dict object as the store below
#       "last_known_agent":     {...},               #   ''
#       "packets": 12, "rejected": 0,                # bounded per-USV diagnostics
#   } }
current_vehicle_state = {}


def vehicle_record(cid, *, create=True):
    """The one authoritative record for a vehicle, created on first contact.

    `last_known_telemetry` / `last_known_agent` intentionally hold the SAME dict objects
    as the per-vehicle stores below, so the record is a view over them rather than a
    second copy that could drift out of sync."""
    rec = current_vehicle_state.get(cid)
    if rec is None:
        if not create:
            return None
        rec = {
            "canonical_id": cid,
            "slug": vehicle_slug(cid),
            "display_name": REGISTRY.default_display_name(cid),
            "raw_latest": None,
            "received_at": None,
            "message_timestamp": None,
            "last_known_telemetry": last_known_telemetry.setdefault(cid, {}),
            "last_known_agent": last_known_agent.setdefault(cid, {}),
            "packets": 0,
            "rejected": 0,
        }
        current_vehicle_state[cid] = rec
    return rec


def fleet_vehicle_ids():
    """Every vehicle the fleet endpoint reports, in a STABLE order: configured vehicles
    first (declaration order), then discovered ones in first-contact order. Stability
    matters because nothing may select or identify a vehicle by list position."""
    ids = [cid for cid in REGISTRY.configured_ids()]
    seen = set(ids)
    for cid in current_vehicle_state:            # dicts preserve insertion order
        if cid not in seen:
            seen.add(cid)
            ids.append(cid)
    return ids


# Newest accepted message send-time per vehicle (epoch seconds). Enforces monotonic
# current-state updates PER USV: a replayed/buffered Scout packet older than the newest
# Scout packet must not overwrite Scout's current state — but it says nothing about SAR,
# whose packets are guarded only against SAR's own newest timestamp. Interleaved arrivals
# from several vehicles are normal and must never block each other.
latest_msg_ts_by_id = {}   # {vehicle_id: float epoch seconds}

# --- Operator-side comms-state transition log (see SYSTEM_INFORMATION_MODEL.md) ---
# The operator backend owns the *arrival-age* view of reachability. A 1s monitor
# records every per-vehicle transition so the UI can draw a comms timeline and the
# thesis can measure total disconnected time — independent of frontend polling.
last_seen_by_id = {}       # {vehicle_id: datetime}
comms_state_by_id = {}     # {vehicle_id: last-logged state}
comms_history_by_id = {}   # {vehicle_id: [ {state, from, ts, since_last_seen_s} ]}

# Last-known telemetry per vehicle (store-and-forward consequence). When the Local
# Agent degrades and drops the telemetry group, the operator must show the LAST KNOWN
# position/heading/etc — NOT invent a home/zero position. The operator owns "current
# state" = last-known composite; absent != reset. Only ever updated from real values.
last_known_telemetry = {}  # {vehicle_id: {lat, lng, heading, groundspeed, battery, ...}}

# Last-known agent reasoning per vehicle (payload.agent.*: current_behaviour,
# decision_reason, current_policy, autonomy_level, …). Same store-and-forward rule as
# telemetry: the Agent page shows the LAST KNOWN reasoning (marked stale) when a packet
# omits the group, never a blank. Only ever updated from a real, non-stale packet.
last_known_agent = {}      # {vehicle_id: {agent reasoning dict}}

# Last-known GROUP snapshots per vehicle (power, failsafe, imu, freshness, mavlink,
# communication, health, mission, service_status, measurements, telemetry).
#
# MERGE SEMANTICS — deliberately group-level, NOT a blanket deep merge (see
# vehicle_telemetry.effective_group for the full rationale):
#   * a group PRESENT in a packet is AUTHORITATIVE and replaces the stored one wholesale,
#     because Scout emits full group snapshots — deep-merging fields would resurrect a
#     reading Scout deliberately stopped sending;
#   * a group ABSENT from a packet is a PARTIAL UPDATE, not a clear: the vehicle's last
#     snapshot is reused and flagged stale, so a mission-only packet can no longer erase
#     power/IMU/freshness and a health-only packet can no longer erase telemetry.
# Strictly per vehicle id: one USV's partial update can never touch another's groups.
last_known_groups = {}     # {vehicle_id: {group_name: dict}}

# Per-vehicle packet-loss estimators over the Local Agent's `communication.seq`. The
# RECEIVER has to do this arithmetic — Scout cannot know which of its own sends never
# arrived (see vehicle_telemetry.PacketLossEstimator for the window/reset semantics).
packet_loss_by_id = {}     # {vehicle_id: vehicle_telemetry.PacketLossEstimator}


def packet_loss_estimator(cid):
    """This vehicle's own estimator, created on first contact. Never shared."""
    est = packet_loss_by_id.get(cid)
    if est is None:
        est = packet_loss_by_id[cid] = vehicle_telemetry.PacketLossEstimator()
    return est

# Change-tracking for first-class agent/mission events (P3). We record a decision or a
# mission-state event ONLY when the value actually changes — never once per status poll
# (a status arrives ~1 Hz; logging every unchanged one would bury the real transitions).
last_agent_decision_by_id = {}   # {vehicle_id: (behaviour, decision_reason, policy)}
last_mission_state_by_id = {}    # {vehicle_id: mission_state}
last_authority_by_id = {}        # {vehicle_id: last event-recorded effective authority}

# --- Control authority (supervisory: who may command the Pixhawk) ---
# Separate from flight mode, and deliberately NOT part of the command queue below —
# it is vehicle state owned by the Scout Flask service (motherpi/services/flask),
# not an operator-issued mission command. The operator backend holds no authority
# state of its own; every read/write is a live, synchronous proxy to the vehicle's own
# GET/POST /agent/control_authority (see set_control_authority / get_control_authority
# further down). VEHICLE_API_BASE is the same "no Configuration API yet" hardcoded
# per-vehicle map already used by Pilot.js's DASHBOARDS / Terminal.js's SSH_TARGETS —
# only vehicles with a real, reachable vehicle-local Flask instance belong here.
#
# THIS IS NOT THE VEHICLE REGISTRY, and the two must not be conflated:
#
#   vehicles.json / REGISTRY  — MONITORING & IDENTITY. Who exists, what canonical id
#       their packets resolve to, what operators see them called. A vehicle listed here
#       is tracked, sorted, selectable, and shows live telemetry, because telemetry is
#       something the vehicle PUSHES to us (POST /agent/status). No address needed.
#
#   VEHICLE_API_BASE (below)  — OUTBOUND COMMAND & API ROUTING. Where this station PULLs
#       from / POSTs to on the vehicle's own network: control authority, Pixhawk mission
#       reads, network-impairment experiment control. Needs a real, verified address.
#
# The two are independent, and BOTH are required for a fully functional vehicle: a
# registry entry alone gives monitoring only. Appearing in vehicles.json therefore does
# NOT make a vehicle commandable — a future USV needs a registry identity AND a verified
# row here before control authority, Pixhawk mission reads or experiment control work.
#
# A registry entry with no VEHICLE_API_BASE row is a normal, supported state: the vehicle
# is monitored, and the vehicle-local endpoints answer 200 available:false rather than
# failing — see get_control_authority / get_pixhawk_mission. Adding a GUESSED address is
# strictly worse than omitting one: an absent entry degrades honestly, while a wrong one
# sends authority and command traffic to whatever host actually holds that IP.
#
# NOTE ON PORTS: 8080 is the vehicle's Flask API (Gunicorn, behind docker-proxy) — the
# ONLY port this map may name. Port 8090 on the same host is the Python Local Agent's
# DIAGNOSTICS server, and it is a trap rather than a clean error: probed live on SAR it
# answers /agent/pixhawk_mission with a full, correct-looking mission, but 404s on
# /agent/control_authority, /agent/experiment/network and /agent/state. A row pointed at
# 8090 would therefore look healthy on the Mission/Map pages while control authority and
# experiment control silently failed — worse than an obviously dead address. Always 8080.
#
# Vehicle telemetry does not travel over this map at all: the Local Agent PUSHES it to the
# operator via POST /agent/status (see receive_agent_status). That push path needs no entry
# here, which is why a vehicle can be fully monitored with no route configured.
VEHICLE_API_BASE = {
    2: "http://10.0.2.10:8080",  # Scout   — motherpi Flask API, over WireGuard
    3: "http://10.0.3.10:8080",  # SAR-001 — verified over WireGuard (wg0 10.0.3.10/16)
}


def vehicle_api_base(vid):
    """Base URL for a vehicle's own Flask API, looked up by CANONICAL id so any accepted
    spelling (3, '3', 'usv-3', 'USV-3', 'SAR-001') resolves to the same entry.

    Returns None for a vehicle with no configured route — callers must render that as an
    honest available:false and must NEVER substitute another vehicle's base URL."""
    return VEHICLE_API_BASE.get(canonical_id(vid))


# --- Local Agent replanning API (port 8090) — a SEPARATE routing surface from Flask (8080) ---
# The replanning lifecycle (planning package, energy decision, FSM, experiment injection,
# runtime config, reset) lives on the Python Local Agent's HTTP server on port 8090, NOT the
# Flask API on 8080 (VEHICLE_API_BASE above). The two are deliberately distinct: Flask 8080
# answers control authority / Pixhawk-mission reads / network-impairment; the Local Agent 8090
# owns everything under /agent/replan/*. A vehicle needs BOTH addresses to be fully driveable,
# and an absent entry here degrades honestly (replanning routes answer supported/reachable:false)
# rather than being guessed onto another host. Same canonical-id lookup, same isolation rule as
# vehicle_api_base — one vehicle's base URL is NEVER substituted for another's.
LOCAL_AGENT_API_BASE = {
    2: "http://10.0.2.10:8090",  # Scout   — Local Agent replanning server, over WireGuard
    3: "http://10.0.3.10:8090",  # SAR-001 — Local Agent replanning server, over WireGuard
}


def local_agent_base(vid):
    """Base URL for a vehicle's Local Agent replanning API (port 8090), by CANONICAL id.
    Returns None for a vehicle with no configured route — callers render supported:false."""
    return LOCAL_AGENT_API_BASE.get(canonical_id(vid))


# Scout's own patchable runtime-config fields (task Section 5). The operator forwards ONLY
# these; an unknown key is dropped rather than passed through to a Scout that would reject the
# whole PATCH. Scout remains the validator of ranges/bounds — the operator does not re-validate.
REPLAN_PATCHABLE_FIELDS = frozenset({
    "autonomous_execution_enabled", "dry_run", "rtl_fallback_enabled",
    "critical_battery_percent", "reserve_margin_percent", "usable_range_m",
    "energy_persistence_count", "max_transaction_retries", "cooldown_s",
})

# Scout's energy-replanning experiment-injection override fields (task Section 6). At least one
# must be supplied; `target_vehicle` is set by the backend to the selected vehicle, never taken
# from the browser, so an injection can only ever target the Scout the operator selected.
REPLAN_EXPERIMENT_FIELDS = frozenset({
    "force_safe_return", "energy_margin_percent", "battery_percent", "duration_s",
})

# Scout's package-consistency verdicts (task handoff). Only CONSISTENT clears replanning
# readiness; the rest are surfaced verbatim so the operator sees WHY Scout fails closed.
PACKAGE_CONSISTENT = "PLANNING_PACKAGE_CONSISTENT"

# --- Persistent event log (BACKEND_ROADMAP #2; Operator-backend-owned) ---
# One server-side, append-only record that replaces the frontend's flatten-from-
# payload feed. Two sources feed it: (1) operator-side comms-state transitions from
# the monitor above (first-class, deterministic), and (2) vehicle-reported events
# forwarded in POST /agent/status.payload.events (deduped so re-sent packets don't
# spam the log). Exposed at GET /api/events. Stable ids + an `acknowledged` field
# model future acknowledgement without inventing the action yet. In-memory (resets
# on restart), like the comms log; durable storage is out of scope here.
MAX_EVENTS = 5000
event_log = []             # [ {id, ts, severity, type, source, vehicle_id, vehicle, message, acknowledged} ]
_event_seq = 0             # monotonic event id (supports later replay / since-id)
_ingested_event_keys = set()  # fingerprints of forwarded vehicle events already stored
# vehicle_names {canonical_id: display name} is seeded from the registry (defined below)


def never_contacted_row(cid):
    """The fleet row for a CONFIGURED vehicle that has not reported yet.

    Replaces the old hardcoded FLEET_TEMPLATE list. That list was half of the alternation
    bug: a live vehicle's row was overwritten by this static template the moment ANOTHER
    vehicle posted, which is why the same vehicle appeared as a complete "SAR-001" one
    second and an empty "USV-3" the next. A template row is now used ONLY for a vehicle
    that has genuinely never made contact — live data always updates that same canonical
    record instead of replacing it with, or adding, a second row."""
    return {
        "id": cid,
        "vehicle_id": vehicle_slug(cid),
        "name": name_of(cid),
        "online": False,
        "status": "UNKNOWN",
        "battery": None,
        "comms": "No data",
        "comm_state": "UNKNOWN",
        "last_seen_age_s": None,
        "last_seen": None,
        "heading": None,
        "speed": None,
        "mission": "Unknown",
        "coverage": None,
        # No position until the vehicle actually reports one — never fabricate a marker.
        "lat": None,
        "lng": None,
        "agent": {},
        "telemetry": {},
        # The SAME field set a contacted vehicle's row carries, empty rather than absent, so
        # no consumer has to branch on "has this vehicle ever reported?" — a missing key and
        # a null value are very different things to a UI that reads them.
        "home": home_block(cid, {}, {}),
        "mission_data": {},
        "communication": {},
        "health": {},
        "mavlink": mavlink_evidence({}),
        "measurements": {},
        "events": [],
        # The SAME canonical blocks a contacted vehicle carries, normalized from an empty
        # payload so every field is an explicit null. A never-contacted vehicle must have
        # the same SHAPE as a live one — a consumer that has to branch on "does this key
        # exist?" is one refactor away from rendering `undefined` at the operator.
        "power": vehicle_telemetry.power_block({}),
        "failsafe": vehicle_telemetry.failsafe_block({}),
        "imu": vehicle_telemetry.imu_block({}),
        "freshness": vehicle_telemetry.freshness_block({}),
        "service_status": vehicle_telemetry.service_status_block({}),
        "leak_sensor": vehicle_telemetry.leak_sensor_block({}),
        "sampling": vehicle_telemetry.sampling_block({}),
        "mission_status": vehicle_telemetry.mission_block({}),
        "agent_summary": vehicle_telemetry.agent_summary({}),
        "link": vehicle_telemetry.link_block({}, None),
        "stale_groups": [],
        "agent_status": {},
        "mission_upload": None,
        "fleet_info": {},
        "raw": None,
        "contacted": False,
    }


# Per-vehicle display names. Seeded from the registry (stable, configured) and updated
# ONLY by that vehicle's own packets — a name reported by one USV can never rename another.
vehicle_names = {cid: REGISTRY.default_display_name(cid) for cid in REGISTRY.configured_ids()}


def _first_present(*vals):
    """First value that is not None (used to merge candidate field spellings)."""
    for v in vals:
        if v is not None:
            return v
    return None


def _age_seconds_from(raw):
    """Seconds since an epoch or ISO-8601 timestamp, or None. Used to convert a
    Scout-provided 'last heartbeat / last message time' into a freshness age."""
    if raw is None or raw == "":
        return None
    ts = extract_message_ts({"timestamp": raw})  # reuses the epoch/ISO parser
    if ts is None:
        return None
    return max(0.0, (datetime.now(timezone.utc).timestamp() - ts))


def mavlink_evidence(payload: dict) -> dict:
    """Normalized MAVLink / Pixhawk-heartbeat evidence for the diagnostics page.

    Read STRICTLY from real link fields Scout forwards — never inferred from GPS or
    arrival age (a HEARTBEAT is its own MAVLink message; GPS position is not proof of
    one). When Scout exposes none of these, every field is None and the operator
    diagnostics render NOT AVAILABLE rather than a fabricated PASS.

    The field spellings now live in vehicle_telemetry.mavlink_block, which is the ONE
    place they are written down. This wrapper used to read `mav.connected` /
    `mav.last_msg_age_s` / `mav.msg_rate_hz` — none of which the Local Agent sends; it
    sends `mavlink_connected`, `mavlink_last_msg_age_s`, `mavlink_msg_rate_hz` — so
    every field came out None and the MAVLink row said NO TELEM against a connected
    autopilot. Older `last_heartbeat` / `last_msg_time` timestamp spellings are still
    accepted here so a pre-update Local Agent keeps working."""
    block = vehicle_telemetry.mavlink_block(payload if isinstance(payload, dict) else {})
    if block["heartbeat_age_s"] is None:
        comm = payload.get("communication", {}) or {}
        health = payload.get("health", {}) or {}
        mav = payload.get("mavlink") if isinstance(payload.get("mavlink"), dict) else {}
        legacy = _age_seconds_from(_first_present(
            mav.get("last_heartbeat"), comm.get("last_heartbeat"),
            health.get("last_heartbeat")))
        if legacy is None:
            legacy = _first_present(health.get("pixhawk_heartbeat_age_s"),
                                    health.get("heartbeat_age_s"))
        if isinstance(legacy, (int, float)):
            block["heartbeat_age_s"] = round(legacy, 2)
    if block["last_msg_age_s"] is None:
        comm = payload.get("communication", {}) or {}
        mav = payload.get("mavlink") if isinstance(payload.get("mavlink"), dict) else {}
        legacy = _age_seconds_from(_first_present(mav.get("last_msg_time"),
                                                  comm.get("mavlink_last_message")))
        if isinstance(legacy, (int, float)):
            block["last_msg_age_s"] = round(legacy, 2)
    return block


# --- Vehicle Home (Pixhawk HOME_POSITION / RTL recovery point) ---
# ONE Scout-owned source of truth: payload.agent.home_status, reported continuously
# (on every status packet) by the Local Agent/Scout Flask. The operator backend is a
# thin, honest mirror of it — it does NOT compute, latch, or reconstruct verification
# itself. In particular a SET_HOME *command* reaching EXECUTED means only "the Local
# Agent successfully called Scout Flask" (command-protocol semantics) — it is NOT proof
# Home was verified, so the command result never writes into this block (see
# _annotate_set_home_result: it only classifies the command's OWN immediate result for
# a toast/pending flash). If Scout stops reporting home_status (restart, disconnect),
# the very next packet's absence of it is what un-verifies the UI — never a value we
# keep asserting on the vehicle's behalf.
HOME_VERIFY_TOLERANCE_M = 5.0     # used only to sanity-check a SET_HOME command's own
                                  # immediate result before calling it a success (see
                                  # _annotate_set_home_result) — never to compute the
                                  # permanent verified state, which is Scout's alone.


def _home_status_source(vid: int, payload: dict):
    """(home_status, stale) for one vehicle. Prefers payload.agent.home_status from
    THIS packet; when this packet's agent group omits it, falls back to the last agent
    block Scout sent (last_known_agent — already maintained by receive_agent_status for
    the Agent page, reused here rather than a second cache) and marks the result stale.
    Returns (None, False) when Scout has never reported it."""
    agent = payload.get("agent") if isinstance(payload.get("agent"), dict) else None
    if agent and isinstance(agent.get("home_status"), dict):
        return agent["home_status"], False
    lk_agent = last_known_agent.get(vid)
    if isinstance(lk_agent, dict) and isinstance(lk_agent.get("home_status"), dict):
        return lk_agent["home_status"], True
    return None, False


def mission_upload_block(vid: int, payload: dict):
    """Scout's live background mission-upload worker state (payload.agent.mission_upload),
    normalized to { active, state, command_id, elapsed_s } — or None when Scout is not
    reporting it (the Mission page then falls back to the queue status alone).

    DELIBERATELY NOT last-known-backed, unlike home_block. `active` is an instant-in-time
    claim that a transfer is running RIGHT NOW; replaying a cached `active: true` after
    Scout goes quiet would leave the UI showing "Executing" forever for an upload that
    died with the link — inventing progress is exactly the failure this station must not
    have. A packet that omits the group, or a vehicle that is not CONNECTED, yields None
    and the UI shows the last real command state instead."""
    agent = payload.get("agent") if isinstance(payload.get("agent"), dict) else None
    mu = agent.get("mission_upload") if isinstance(agent, dict) else None
    if not isinstance(mu, dict):
        return None
    if comms_state_by_id.get(vid, "UNKNOWN") != "CONNECTED":
        return None
    elapsed = mu.get("elapsed_s")
    cid = mu.get("command_id")
    return {
        "active": bool(mu.get("active")),
        "state": str(mu["state"]).upper() if mu.get("state") not in (None, "") else None,
        # Kept as a string: command ids are uuids, and the UI matches them by equality.
        "command_id": str(cid) if cid not in (None, "") else None,
        "elapsed_s": round(float(elapsed), 1) if isinstance(elapsed, (int, float)) else None,
        "source": "scout",
    }


def home_block(vid: int, payload: dict, telemetry: dict):
    """The fleet-payload `home` block for one vehicle — Scout's own fields, verbatim,
    never independently recomputed. `verified`/`ready_for_auto`/`ready_for_rtl` are
    forced False whenever the status is stale (last-known fallback, or the vehicle
    itself isn't CONNECTED): a stale status is displayed as unverified, never silently
    trusted as still current."""
    hs, is_fallback = _home_status_source(vid, payload)
    stale = is_fallback or comms_state_by_id.get(vid, "UNKNOWN") != "CONNECTED"
    if hs is None:
        return {
            "source": "scout", "available": False, "reachable": None, "home_position": None,
            "lat": None, "lng": None, "verified": False, "verified_at": None,
            "verification_method": None, "verification_distance_m": None,
            "verification_recovery": None,
            "ready_for_auto": False, "ready_for_rtl": False,
            "reason": "Scout does not report Home status yet.", "stale": False,
        }
    home_position = hs.get("home_position") if isinstance(hs.get("home_position"), dict) else None
    lat = _mission_coord(home_position.get("latitude"), home_position.get("lat")) if home_position else None
    lng = _mission_coord(home_position.get("longitude"), home_position.get("lng")) if home_position else None
    return {
        "source": "scout",
        "available": lat is not None and lng is not None,
        "reachable": hs.get("reachable"),
        "home_position": home_position,
        "lat": lat, "lng": lng,
        "verified": bool(hs.get("verified")) and not stale,
        "verified_at": hs.get("verified_at") if not stale else None,
        "verification_method": hs.get("verification_method"),
        "verification_distance_m": hs.get("verification_distance_m"),
        # Scout's read-only VERIFICATION RECOVERY evidence, verbatim ({state, reason,
        # checked_at}) — how the current verification survived a Local Agent restart. It is
        # PROVENANCE, never a second source of verification: `verified` above is the only
        # thing that decides verified/unverified, and a RECOVERED recovery state alongside
        # `verified:false` still reads UNVERIFIED. Passed through rather than summarized so
        # the diagnostics page can show Scout's own sentence for why recovery was or was not
        # possible.
        "verification_recovery": (hs.get("verification_recovery")
                                  if isinstance(hs.get("verification_recovery"), dict) else None),
        "ready_for_auto": bool(hs.get("ready_for_auto")) and not stale,
        "ready_for_rtl": bool(hs.get("ready_for_rtl")) and not stale,
        "reason": ("Scout has not confirmed Home status recently — treating as unverified."
                   if stale else hs.get("reason")),
        "stale": stale,
    }


def normalize_agent_message(message: dict, cid=None, received_at=None) -> dict:
    """Normalize ONE vehicle's last accepted packet into its fleet row.

    Accepts both:
    1. Envelope format:
       {"message_type": "...", "source": "...", "payload": {...}}

    2. Direct payload format:
       {"usv_id": ..., "comm_state": ..., "telemetry": {...}}

    `cid` / `received_at` are that vehicle's OWN canonical id and arrival time. They used
    to be read from a single global `latest_agent_received_at`, which meant every vehicle's
    comm-state was derived from whenever ANY vehicle last posted: a silent USV looked
    CONNECTED because a different one was alive, and a live USV could not age on its own
    clock. Freshness is per-USV and nothing else.
    """
    if "payload" in message and isinstance(message["payload"], dict):
        payload = message["payload"]
        envelope = message
    else:
        payload = message
        envelope = {}

    # Identity comes from the caller (the per-USV record this packet was stored under), so
    # one packet can only ever describe one vehicle. Re-deriving it here is a fallback for
    # direct callers/tests; it never silently defaults to another vehicle.
    usv_id = cid if cid is not None else extract_usv_id(message)

    # PARTIAL-UPDATE PROTECTION. Every group is resolved through effective_group against
    # THIS vehicle's own last-known snapshots: a group this packet carries wins outright,
    # a group it omits falls back to the vehicle's previous one and is listed in
    # `stale_groups`. Without this, a packet carrying only `mission` blanked power, IMU,
    # freshness and health for one poll and the whole page flickered to "—".
    def group(name):
        return vehicle_telemetry.effective_group(last_known_groups, usv_id, payload, name)

    telemetry, _ = group("telemetry")
    mission, _ = group("mission")
    communication, _ = group("communication")
    health, _ = group("health")
    measurements, _ = group("measurements")
    fleet_info = payload.get("fleet", {}) or {}
    events = payload.get("events", []) or []
    agent_reasoning = payload.get("agent", {}) or {}

    # The payload the canonical block normalizers see: this packet, with any group it
    # omitted filled in from that vehicle's last-known snapshot. `agent` is deliberately
    # taken from the existing last_known_agent store rather than a second cache.
    effective_payload = dict(payload)
    stale_groups = []
    for _name in vehicle_telemetry.CARRIED_GROUPS:
        _value, _stale = group(_name)
        if _value:
            effective_payload[_name] = _value
            if _stale:
                stale_groups.append(_name)
    if not agent_reasoning and last_known_agent.get(usv_id):
        effective_payload["agent"] = last_known_agent[usv_id]
        stale_groups.append("agent")

    comm_state = payload.get("comm_state", "UNKNOWN")

    # Last-known fallback (store-and-forward): when this packet omits/zeroes a field,
    # use the last real value the vehicle reported rather than inventing one. Never
    # substitute a home/zero position — an absent position stays None so the UI shows
    # "no fix"/last-known and the map does not plot a fabricated marker.
    lk = last_known_telemetry.get(usv_id, {})
    battery = telemetry.get("battery")
    # A MAVLink battery_remaining of -1 means "unknown/unavailable this packet" — it is a
    # transient absence, NOT a real 0% or a deliberate clear. Treat it exactly like a missing
    # field: fall back to the last real value so a single -1 packet cannot flip a valid 97% to
    # "—" and back on the next poll (the two-second telemetry flicker). See receive_agent_status,
    # which also refuses to STORE -1 into last_known_telemetry so the fallback stays valid.
    if battery is None or battery == -1:
        battery = lk.get("battery", payload.get("battery"))

    lat = telemetry.get("lat") or payload.get("lat") or lk.get("lat")
    lng = telemetry.get("lng") or payload.get("lng") or lk.get("lng")

    def age_seconds(when):
        if not when:
            return None
        t = when if isinstance(when, datetime) else datetime.fromisoformat(when)
        return (datetime.now(timezone.utc) - t).total_seconds()

    # THIS vehicle's own arrival time — never the fleet-wide "someone posted" timestamp.
    if received_at is None:
        received_at = last_seen_by_id.get(usv_id)
    age = age_seconds(received_at)
    if age is None:
        online = False
        comm_state = "UNKNOWN"
    elif age > DISCONNECTED_AFTER_SECONDS:
        online = False
        comm_state = "DISCONNECTED"
    elif age > PARTITIONED_AFTER_SECONDS:
        online = True
        comm_state = "PARTITIONED"
    else:
        # Operator-side comm-state is arrival-age-derived, NOT the vehicle's
        # self-assessment: a fresh arrival means the operator link is up now, even
        # if a replayed/buffered payload self-reports DISCONNECTED. (The vehicle's
        # own link view stays available under `communication`/`raw`.)
        online = True
        comm_state = "CONNECTED"

    heading = telemetry.get("heading")
    if heading is None:
        heading = lk.get("heading")
    speed = telemetry.get("groundspeed", telemetry.get("speed"))
    if speed is None:
        speed = lk.get("groundspeed", lk.get("speed"))

    # Canonicalize the live flight mode for display with the SAME normalizer command
    # verification uses, so a raw numeric custom_mode (11) renders as its name (RTL) and the
    # live-mode chip can never disagree with a command's verified mode. Only rewritten when
    # recognised; the untouched raw value is kept as mode_raw. Scout usually sends the name.
    if isinstance(telemetry, dict) and telemetry.get("mode") is not None:
        canon_mode = normalize_rover_mode(telemetry.get("mode"))
        if canon_mode is not None and canon_mode != telemetry.get("mode"):
            telemetry = {**telemetry, "mode": canon_mode, "mode_raw": telemetry.get("mode")}

    return {
        "id": usv_id,
        # Stable string form of the SAME canonical identity ("usv-2"). Published so any
        # consumer can key on an id that is safe in a URL; it is not a second identity.
        "vehicle_id": vehicle_slug(usv_id),
        # Per-USV display name. Read from name_of() — the sticky per-vehicle name this
        # vehicle itself last reported (or its configured one) — so it cannot flip between
        # a configured placeholder and a live callsign depending on who posted last.
        "name": name_of(usv_id),
        "contacted": True,
        "online": online,
        "status": mission.get("mission_state", payload.get("mission_state", "UNKNOWN")),
        "battery": battery if battery != -1 else None,
        "comms": comm_state,
        "comm_state": comm_state,
        "last_seen_age_s": round(age, 1) if age is not None else None,
        "heading": heading,
        "speed": speed,
        "mission": mission.get("mission_state", payload.get("mission", "Unknown")),
        "coverage": payload.get("coverage"),
        # FUTURE EXTENSION POINT (mission-revision notification / auto-refresh): this dict is
        # the SINGLE place the fleet payload's per-vehicle shape is built, so a lightweight
        # revision signal Scout may start reporting — active_revision_id / active_route_hash /
        # mission_changed_at — is surfaced HERE (read it off `mission`/`payload`, e.g.
        # mission.get("active_revision_id")). The frontend already refetches the mission via
        # Map.js fetchPixhawkMission(); onFleet only has to compare this field to trigger it.
        # NOT added yet — see BACKEND_ROADMAP.md; the read-back proxy has its own passthrough
        # list in _scout_mission_read for the same fields on the pixhawk-mission response.
        "lat": lat,
        "lng": lng,
        # Vehicle Home (Pixhawk HOME_POSITION / RTL recovery point) + the operator's
        # read-back verification record. Distinct from `lat`/`lng` (the vehicle's own
        # position) — the map plots them as two separate markers.
        "home": home_block(usv_id, payload, telemetry),
        # Groups forwarded verbatim, now resolved through the partial-update guard above
        # (a packet that omits one keeps this vehicle's last snapshot rather than blanking).
        "mission_data": mission,
        "communication": communication,
        "health": health,
        "mavlink": mavlink_evidence(effective_payload),
        "measurements": measurements,
        "events": payload.get("events", []) or [],
        # --- CANONICAL NORMALIZED BLOCKS (vehicle_telemetry.py) -----------------------
        # One normalization for every page. Before these existed the Vehicle page had no
        # backend field to read for power/failsafe/IMU/freshness/services/link
        # diagnostics, so ~15 rows were hardcoded placeholders while Scout was sending
        # the data every second. Each block is honest about absence: a field Scout does
        # not send is null, and 0 survives as 0.
        "power": vehicle_telemetry.power_block(effective_payload, telemetry),
        "failsafe": vehicle_telemetry.failsafe_block(effective_payload),
        "imu": vehicle_telemetry.imu_block(effective_payload),
        "freshness": vehicle_telemetry.freshness_block(effective_payload),
        "service_status": vehicle_telemetry.service_status_block(effective_payload),
        "leak_sensor": vehicle_telemetry.leak_sensor_block(effective_payload),
        "sampling": vehicle_telemetry.sampling_block(effective_payload),
        "mission_status": vehicle_telemetry.mission_block(effective_payload),
        "agent_summary": vehicle_telemetry.agent_summary(effective_payload),
        # Scout↔Operator link diagnostics (RTT / WireGuard / operator_connected / seq) plus
        # the OPERATOR-side packet-loss estimate. Diagnostic only: `comm_state` above stays
        # arrival-age derived, which is the thesis's degradation model.
        "link": vehicle_telemetry.link_block(
            effective_payload,
            packet_loss_estimator(usv_id).estimate(time.time()) if usv_id is not None else None),
        # Which groups in THIS row came from a previous packet rather than this one, so the
        # UI can mark them last-known instead of presenting them as current.
        "stale_groups": stale_groups,
        # Agent reasoning (payload.agent.*) forwarded verbatim for the Agent page:
        # current_behaviour, decision_reason, current_policy, autonomy_level,
        # current_communication_state, current_mission_state, buffer_usage,
        # last_operator_command. Falls back to the last-known reasoning (marked stale by
        # the frontend via comm-state) when a degraded packet omits the group.
        "agent_status": agent_reasoning or last_known_agent.get(usv_id, {}),
        # Live background mission-upload state from Scout's upload worker
        # (payload.agent.mission_upload). Normalized to a stable shape so the Mission page
        # never has to guess field spellings; None when Scout is not reporting it.
        "mission_upload": mission_upload_block(usv_id, payload),
        "fleet_info": fleet_info,
        "agent": {
            "groups": payload.get("groups", []),
            "source": envelope.get("source", payload.get("source")),
            "target": envelope.get("target", payload.get("target")),
            "message_type": envelope.get("message_type", "status"),
            "schema_version": envelope.get("schema_version", "unknown"),
            "timestamp": envelope.get("timestamp", time.time()),
        },
        "telemetry": telemetry,
        "raw": message,
        # This vehicle's own last contact — not "when the fleet last heard from anyone".
        "last_seen": received_at.isoformat() if isinstance(received_at, datetime) else received_at,
    }


# Identity fields a status packet may carry, in priority order. Scout's Local Agent sends
# payload.usv_id = 2 with envelope source "usv-2"; another vehicle may send "3", "usv-3",
# "USV-3" or its callsign. Every spelling is folded to ONE canonical id by the registry.
# `name` is deliberately absent: a display name is not an identity, and resolving by it
# would let a renamed vehicle jump records. The envelope `source` is the last resort — it
# is the only identity a payload-less/degraded envelope carries.
_PACKET_ID_KEYS = ("usv_id", "vehicle_id", "id", "usvId", "vehicleId")


def extract_usv_id(message: dict):
    """Canonical vehicle id of a status message, or None when it identifies no vehicle.

    Returning None (instead of the old silent `2` fallback) is the point: an unidentified
    packet must be rejected, never merged into whichever vehicle happens to be first in
    the registry."""
    if not isinstance(message, dict):
        return None
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else message
    for key in _PACKET_ID_KEYS:
        cid = canonical_id(payload.get(key))
        if cid is not None:
            return cid
    for key in _PACKET_ID_KEYS:
        cid = canonical_id(message.get(key))
        if cid is not None:
            return cid
    return canonical_id(message.get("source") or payload.get("source"))


def extract_name(message: dict):
    """Display name from either envelope or direct-payload form (or None)."""
    payload = message.get("payload", message) if isinstance(message, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    return payload.get("name") or payload.get("vehicle_name")


def parse_vehicle_id(raw):
    """Canonical vehicle id from a path/query/body value in ANY accepted spelling —
    '2', 2, 'usv-2', 'USV-2' or a configured alias — or -1 when it names no vehicle the
    station knows.

    Deliberately resolves only to EXISTING vehicles (configured, or already in contact):
    a URL, query string or request body must never bring a vehicle into existence. A uuid
    or typo therefore still parses to the -1 sentinel the routes 404 on, exactly as before.
    Vehicles are discovered in one place only — an identified status packet."""
    cid = canonical_id(raw)
    if cid is None or not (REGISTRY.is_configured(cid) or cid in current_vehicle_state):
        return -1
    return cid


def extract_message_ts(message: dict):
    """Best-effort send-time (epoch seconds) of a status message, for the monotonic
    current-state guard. Reads the envelope `timestamp` (local_agent sends
    `time.time()`), falling back to a payload timestamp. Accepts epoch numbers,
    numeric strings, or ISO-8601. Returns None if the message carries no time."""
    if not isinstance(message, dict):
        return None
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    raw = (message.get("timestamp") or message.get("ts")
           or payload.get("timestamp") or payload.get("timestamp_s") or payload.get("ts"))
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return float(raw)  # numeric string
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def name_of(vid) -> str:
    """This vehicle's display name: the last name IT reported, else its configured name,
    else a readable fallback. Per-USV and sticky — no other vehicle's packet can change it."""
    return vehicle_names.get(vid) or REGISTRY.default_display_name(vid)


def derive_comm_state(age_s):
    """Operator-side comm-state from arrival age (same thresholds as normalize)."""
    if age_s is None:
        return "UNKNOWN"
    if age_s > DISCONNECTED_AFTER_SECONDS:
        return "DISCONNECTED"
    if age_s > PARTITIONED_AFTER_SECONDS:
        return "PARTITIONED"
    return "CONNECTED"


def record_comms_state(vid: int, state: str, ts: datetime, age_s):
    """Append a transition only when the state actually changes."""
    prev = comms_state_by_id.get(vid)
    if state == prev:
        return None
    comms_state_by_id[vid] = state
    entry = {
        "state": state,
        "from": prev,
        "ts": ts.isoformat(),
        "since_last_seen_s": round(age_s, 1) if age_s is not None else None,
    }
    comms_history_by_id.setdefault(vid, []).append(entry)
    print(f"[COMMS] {vehicle_slug(vid)}: {prev} -> {state}")
    _emit_comms_event(vid, prev, state, ts)
    return entry


def evaluate_comms_transitions():
    """Re-derive each tracked vehicle's comm-state and log any change."""
    now = datetime.now(timezone.utc)
    for vid, seen in list(last_seen_by_id.items()):
        age = (now - seen).total_seconds()
        record_comms_state(vid, derive_comm_state(age), now, age)


async def _comms_monitor_loop():
    while True:
        try:
            evaluate_comms_transitions()
            expire_commands()
        except Exception as exc:  # keep the loop alive
            print("[COMMS-MONITOR] error:", exc)
        await asyncio.sleep(1)


# --- Event store (see the "Persistent event log" block above) ---

def _append_event(*, severity, message, etype, source, vehicle_id=None,
                  vehicle=None, ts=None, detail=None):
    """Append one event to the server-side log and return it. `detail` is optional
    structured context (e.g. a command event's command_id/type/source/stage/outcome) the
    Events page renders as an expandable detail view — the message string stays the
    human-readable summary."""
    global _event_seq
    _event_seq += 1
    entry = {
        "id": _event_seq,
        "ts": ts or datetime.now(timezone.utc).isoformat(),
        "severity": severity,          # info|caution|warning|emergency or None (UNSPEC)
        "type": etype,                 # e.g. "comms", "vehicle"
        "source": source,              # "operator-backend" or a vehicle/agent id
        "vehicle_id": vehicle_id,
        "vehicle": vehicle,
        "message": message,
        "detail": detail,              # structured, type-specific context (or None)
        "acknowledged": False,         # modelled now; POST ack endpoint is a later item
    }
    event_log.append(entry)
    if len(event_log) > MAX_EVENTS:
        del event_log[0:len(event_log) - MAX_EVENTS]
    # The console echo must never be able to take down the request that logged the event.
    # Event messages carry text this backend does not own — Scout's `start_block_reason`, a
    # publish error, a vehicle name — and the hidden launcher redirects stdout to
    # logs/operator.log, where Python uses the Windows locale encoding (cp1252 here), not the
    # console's UTF-8. One unencodable character in that text would raise UnicodeEncodeError
    # INSIDE the handler and turn a completed transaction into an HTTP 500. Keeping our own
    # message text ASCII (see the "Mission state:" event below) is the first line of defence;
    # this is the second, for text that arrives from elsewhere.
    import sys
    line = f"[EVENT] #{entry['id']} {severity or 'unspec'} {source}: {message}"
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode(enc, "replace").decode(enc, "replace"))
    return entry


def _comms_event_for(prev, state):
    """Deterministic (severity, message) for an operator-side comms transition."""
    if state == "DISCONNECTED":
        return ("warning", "Communication lost")
    if state == "PARTITIONED":
        return ("caution", "Communication partitioned")
    if state == "CONNECTED":
        if prev is None:
            return ("info", "First contact established")
        return ("info", "Communication restored")
    return None  # UNKNOWN / other → not an operator event


def _emit_comms_event(vid, prev, state, ts):
    """Turn a comms-state transition into a first-class event."""
    sev_msg = _comms_event_for(prev, state)
    if sev_msg is None:
        return
    severity, message = sev_msg
    _append_event(
        severity=severity, message=message, etype="comms",
        source="operator-backend", vehicle_id=vid, vehicle=name_of(vid),
        ts=ts.isoformat(),
    )


# Typed vehicle/agent events (e.g. the Local Agent's own comm-state notifications)
# carry a `type` but no severity/message field, so the generic path would render them
# as raw JSON at UNSPEC severity. Map the known types to (severity, message) so the
# Events page shows a clean row. Keeps the event's own `type` and `source`.
TYPED_EVENT_MAP = {
    "comm_recovered":    ("info",    "Local agent communication recovered"),
    "comm_restored":     ("info",    "Local agent communication recovered"),
    "comm_connected":    ("info",    "Local agent communication recovered"),
    "comm_partitioned":  ("caution", "Local agent communication partitioned"),
    "comm_partition":    ("caution", "Local agent communication partitioned"),
    "comm_degraded":     ("caution", "Local agent communication degraded"),
    "comm_lost":         ("warning", "Local agent communication lost"),
    "comm_disconnected": ("warning", "Local agent communication lost"),
}


def normalize_typed_event(ev):
    """(severity, message) for a known typed agent event, else None.
    Appends the reported previous state (`detail.from`) when present, so the row is
    informative without ever falling back to raw JSON."""
    if not isinstance(ev, dict):
        return None
    mapped = TYPED_EVENT_MAP.get(str(ev.get("type") or "").lower())
    if not mapped:
        return None
    severity, message = mapped
    detail = ev.get("detail")
    if isinstance(detail, dict) and detail.get("from"):
        message = f"{message} (from {detail['from']})"
    return severity, message


def derive_event_severity(ev):
    """Severity of a forwarded vehicle event, or None when it carries no level.
    Mirrors the frontend `evSeverity` (lib/ui.js) so it is deterministic and the
    UI never has to re-guess: the backend decides once and stores it."""
    if isinstance(ev, dict):
        raw = str(ev.get("severity") or ev.get("level")
                  or ev.get("priority") or ev.get("sev") or "").lower()
    else:
        raw = ""
    if not raw:
        return None
    if raw.startswith("emerg") or raw in ("critical", "fatal"):
        return "emergency"
    if raw.startswith("warn"):
        return "warning"
    if raw.startswith("caut") or raw in ("alert", "major"):
        return "caution"
    if raw.startswith("info") or raw in ("notice", "debug", "minor"):
        return "info"
    return None


def extract_event_message(ev):
    """Human title of a forwarded event (mirrors frontend `evText`)."""
    if ev is None:
        return ""
    if isinstance(ev, str):
        return ev
    if isinstance(ev, list):
        return " • ".join(str(x) for x in ev)
    if isinstance(ev, dict):
        for k in ("title", "message", "text", "event", "name", "action"):
            if ev.get(k):
                return str(ev[k])
        return json.dumps(ev, sort_keys=True)
    return str(ev)


def to_iso(raw, now):
    """Coerce an event timestamp to ISO-8601 so the whole log is one format.
    Accepts epoch seconds (number or numeric string — local_agent sends time.time())
    or an existing ISO string; anything unparseable falls back to arrival time. Without
    this, epoch-float stamps reach the UI as unparseable strings (wrong order + label)."""
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(raw, timezone.utc).isoformat()
        except (OSError, ValueError, OverflowError):
            return now.isoformat()
    s = str(raw)
    try:
        return datetime.fromtimestamp(float(s), timezone.utc).isoformat()  # numeric epoch string
    except (ValueError, OSError, OverflowError):
        pass
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))  # already ISO — validate, keep
        return s
    except ValueError:
        return now.isoformat()


def event_timestamp(ev, now):
    """The event's own timestamp (normalized to ISO) if it carries one, else arrival."""
    if isinstance(ev, dict):
        raw = (ev.get("timestamp") or ev.get("time") or ev.get("ts")
               or ev.get("created_at") or ev.get("date"))
        if raw is not None and raw != "":
            return to_iso(raw, now)
    return now.isoformat()


def event_fingerprint(vid, ev):
    """Stable identity for a forwarded event so re-sent packets ingest once.
    Prefers an explicit id, else (own timestamp + message); untimestamped repeats
    of identical content collapse to one entry (a log, not per-packet spam)."""
    if isinstance(ev, dict):
        if ev.get("id") is not None:
            return f"{vid}|id={ev['id']}"
        stamp = (ev.get("timestamp") or ev.get("time") or ev.get("ts")
                 or ev.get("created_at") or ev.get("date") or "")
        return f"{vid}|{stamp}|{extract_event_message(ev)}"
    return f"{vid}|{ev}"


def ingest_payload_events(vid, message, now):
    """Store any new vehicle-reported events from a POST /agent/status payload."""
    payload = message.get("payload", message) if isinstance(message, dict) else {}
    if not isinstance(payload, dict):
        return
    events = payload.get("events") or []
    if not isinstance(events, list):
        return
    for ev in events:
        key = event_fingerprint(vid, ev)
        if key in _ingested_event_keys:
            continue
        _ingested_event_keys.add(key)
        etype = (ev.get("type") if isinstance(ev, dict) else None) or "vehicle"
        source = (ev.get("source") if isinstance(ev, dict) else None) or vehicle_slug(vid)
        # Known typed events (e.g. local_agent comm notifications) get a clean
        # severity+message; everything else keeps the generic derivation.
        typed = normalize_typed_event(ev)
        if typed is not None:
            severity, message = typed
        else:
            severity, message = derive_event_severity(ev), extract_event_message(ev)
        _append_event(
            severity=severity, message=message,
            etype=etype, source=source,
            vehicle_id=vid, vehicle=name_of(vid),
            ts=event_timestamp(ev, now),
        )


def _clean(val):
    """A displayable value, or None for absent/placeholder (None/""/'unknown'/'none')."""
    if val is None:
        return None
    s = str(val).strip()
    if s == "" or s.lower() in ("unknown", "none", "n/a", "null"):
        return None
    return s


def record_agent_changes(vid, payload, now):
    """Record first-class events for agent-decision and mission-state CHANGES (P3).

    Emitted only when the value actually changes vs. the last recorded one — never once
    per status poll — so the event log shows real transitions with timestamps + reasons,
    not a flood of unchanged rows. The agent-decision key is (current_behaviour,
    decision_reason, current_policy) from payload.agent.*; the mission key is
    mission.mission_state. Reasons are taken verbatim from the agent (never invented)."""
    if not isinstance(payload, dict):
        return
    agent = payload.get("agent") or {}
    mission = payload.get("mission") or {}
    name = name_of(vid)

    if isinstance(agent, dict) and agent:
        behaviour = _clean(agent.get("current_behaviour") or agent.get("behaviour")
                           or agent.get("behavior"))
        reason = _clean(agent.get("decision_reason") or agent.get("decision_rationale"))
        policy = _clean(agent.get("current_policy") or agent.get("communication_policy"))
        key = (behaviour, reason, policy)
        prev = last_agent_decision_by_id.get(vid)
        # Only meaningful once the agent actually reports a behaviour/decision at least once.
        if key != prev and (behaviour or reason):
            last_agent_decision_by_id[vid] = key
            if prev is not None:  # skip the very first observation (not a transition)
                label = behaviour or "decision updated"
                msg = f"Agent decision: {label}"
                if reason:
                    msg = f"{msg} — {reason}"
                _append_event(severity="info", message=msg, etype="agent",
                              source=vehicle_slug(vid), vehicle_id=vid, vehicle=name,
                              ts=now.isoformat())

    if isinstance(mission, dict) and mission:
        mstate = _clean(mission.get("mission_state"))
        prev_m = last_mission_state_by_id.get(vid)
        if mstate and mstate != prev_m:
            last_mission_state_by_id[vid] = mstate
            if prev_m is not None:
                _append_event(severity="info",
                              # ASCII "->" on purpose: every event message is echoed to the
                              # console by _append_event, and the default Windows code page
                              # (cp1252) cannot encode "→" — it raises UnicodeEncodeError and
                              # takes the request down. Keep console-bound text ASCII-only.
                              message=f"Mission state: {prev_m} -> {mstate}",
                              etype="mission", source=vehicle_slug(vid),
                              vehicle_id=vid, vehicle=name, ts=now.isoformat())


# --- Command queue (BACKEND_ROADMAP: reverse/control Path E; Operator-backend-owned) ---
# The smallest safe Operator → Scout command path. The operator backend is the queue's
# source of truth: it stores command records, gates them on the operator-side comm-state,
# hands pending ones to the Local Agent on next contact, and records the Agent's result.
# It NEVER fabricates execution — only a Local Agent result can mark a command EXECUTED
# (SYSTEM_INFORMATION_MODEL: the backend stores operator-side records, it does not decide
# what the vehicle did). In-memory like event_log / comms history (resets on restart).
#
# Lifecycle owners:
#   QUEUED  — backend, on create
#   SENT    — backend, when the Agent fetches it (a claim; at-least-once redelivery)
#   ACCEPTED/EXECUTED/REJECTED/FAILED — Local Agent only, via the result endpoint
#   EXPIRED — backend, when a non-terminal command passes its TTL (monitor loop)
# Vehicle/Pixhawk modes are real ArduRover modes (MANUAL, AUTO, HOLD, LOITER, GUIDED,
# RTL). MISSION_PAUSE/MISSION_RESUME are agent mission commands — NOT Pixhawk modes —
# and are kept deliberately distinct. ARM/DISARM are the safety pair.
COMMAND_TYPES = {
    "SET_MODE_AUTO", "SET_MODE_MANUAL", "SET_MODE_HOLD", "SET_MODE_LOITER",
    "SET_MODE_GUIDED", "RTL", "MISSION_PAUSE", "MISSION_RESUME",
    "ARM", "DISARM", "SET_HOME",
    # Mission-management commands (BACKEND_ROADMAP mission-upload workflow). MISSION_UPLOAD
    # writes a validated waypoint mission to the Pixhawk; MISSION_CLEAR wipes it. Both are
    # verified by a Scout read-back (observed count/hash vs the operator's expected), never
    # by transport success alone — see _annotate_mission_upload_result.
    "MISSION_UPLOAD", "MISSION_CLEAR",
}
# Arming touches the motors, so both ARM and DISARM ALWAYS require an explicit
# confirm:true (independent of comm-state) — the backend rejects them otherwise.
# ARM is the higher risk of the two (the vehicle can move under power once armed);
# its record carries a caution warning. Still just the queue — no execution here.
# MISSION_UPLOAD/MISSION_CLEAR overwrite the flight controller's stored mission, so they
# join the confirm-required set (readback-verified, same treatment as SET_HOME).
CONFIRM_REQUIRED_TYPES = {"ARM", "DISARM", "SET_HOME", "MISSION_UPLOAD", "MISSION_CLEAR"}
RISK_WARNING = {
    "ARM": "High-risk: the vehicle can move under power once armed. Confirmed by operator.",
    "DISARM": "Disarms the vehicle (motors off). Confirmed by operator.",
    "SET_HOME": "Sets the Pixhawk HOME / RTL recovery point to the Scout's current position. Confirmed by operator.",
    "MISSION_UPLOAD": "Overwrites the mission stored on the Pixhawk. Verified by read-back. Confirmed by operator.",
    "MISSION_CLEAR": "Clears the mission stored on the Pixhawk. Verified by read-back. Confirmed by operator.",
}

# --- Canonical ArduPilot Rover flight-mode normalization -------------------------------
# A Rover reports its flight mode as a numeric custom_mode over MAVLink (HEARTBEAT.custom_
# mode). Depending on where in the Scout → Local Agent → Operator chain a value is read,
# the SAME mode can arrive three different ways: the raw number (11), its numeric string
# ("11"), or the already-resolved name ("RTL"/"rtl"). The verification bug behind
# "Pixhawk reported mode 11, not RTL." was a direct string compare of a numeric custom_mode
# against the name "RTL". This ONE table + normalizer is the canonical mapping every
# consumer uses (command verification, event rendering, live-mode display) so those three
# representations always compare equal. Numbers are ArduPilot Rover's Mode::Number enum.
ROVER_MODE_NUMBER_TO_NAME = {
    0: "MANUAL", 1: "ACRO", 3: "STEERING", 4: "HOLD", 5: "LOITER", 6: "FOLLOW",
    7: "SIMPLE", 8: "DOCK", 9: "CIRCLE", 10: "AUTO", 11: "RTL", 12: "SMART_RTL",
    15: "GUIDED", 16: "INITIALISING",
}
ROVER_MODE_NAMES = set(ROVER_MODE_NUMBER_TO_NAME.values())

# Outcome vocabulary for a canonical mode comparison — distinguishes a genuine vehicle-side
# mismatch (a different KNOWN mode) from a mere representation gap (an unrecognised value),
# so a normalization miss never masquerades as a vehicle rejection.
MODE_VERIFY_VERIFIED = "VERIFIED"                       # canonical expected == canonical observed
MODE_VERIFY_FAILED = "FAILED"                           # both known, genuinely different modes
MODE_VERIFY_UNKNOWN = "UNKNOWN_MODE_REPRESENTATION"     # observed present but not recognised
MODE_VERIFY_UNVERIFIED = "EXECUTED_UNVERIFIED"          # nothing observed to compare against


def normalize_rover_mode(mode):
    """Canonical upper-case ArduPilot Rover mode name for a value Scout may report as a
    numeric custom_mode (11), a numeric string ("11") or an already-resolved name ("RTL",
    "rtl", "Rtl"). Returns None when the value is absent, empty, or NOT a mode this table
    recognises — an unknown representation is never silently coerced into a name."""
    if mode is None or isinstance(mode, bool):
        return None
    if isinstance(mode, int):
        return ROVER_MODE_NUMBER_TO_NAME.get(mode)
    s = str(mode).strip()
    if s == "":
        return None
    if s.isdigit():
        return ROVER_MODE_NUMBER_TO_NAME.get(int(s))
    name = s.upper()
    return name if name in ROVER_MODE_NAMES else None


def verify_mode_match(expected, observed):
    """Compare a mode command's expected state against the vehicle's observed mode, each
    canonicalized first, and return an outcome from MODE_VERIFY_*. The whole point is that
    a numeric custom_mode (11) and its name ("RTL") are the SAME mode, so
    verify_mode_match("RTL", 11) is VERIFIED — Scout authority is not overturned by a
    representation difference. A genuinely different KNOWN mode (RTL vs MANUAL) is FAILED;
    an observed value that cannot be normalized is UNKNOWN_MODE_REPRESENTATION (not a
    rejection); no observed value at all is EXECUTED_UNVERIFIED. Raw values stay the
    caller's to preserve as observed_raw."""
    exp = normalize_rover_mode(expected)
    obs = normalize_rover_mode(observed)
    if exp is not None and obs is not None:
        return MODE_VERIFY_VERIFIED if exp == obs else MODE_VERIFY_FAILED
    if observed is None or (isinstance(observed, str) and observed.strip() == ""):
        return MODE_VERIFY_UNVERIFIED
    return MODE_VERIFY_UNKNOWN

# Normalized command source (who authored the record). OPERATOR is the human at this
# station; LOCAL_AGENT / MISSION_AGENT are autonomy-authored records the contract wants
# preserved and forwarded even though the operator queue only creates OPERATOR ones today.
COMMAND_SOURCES = {"OPERATOR", "LOCAL_AGENT", "MISSION_AGENT"}


def normalize_source(raw):
    """Normalize a source/created_by value onto COMMAND_SOURCES. Unknown/blank → OPERATOR
    (conservative: an operator-station record is operator-authored unless it clearly says
    otherwise), so older records with only a free-text created_by still carry a valid source."""
    s = str(raw or "").upper().strip()
    if s in COMMAND_SOURCES:
        return s
    if "MISSION" in s:
        return "MISSION_AGENT"
    if "AGENT" in s or "LOCAL" in s or "AUTONOM" in s:
        return "LOCAL_AGENT"
    return "OPERATOR"
COMMAND_TTL_S = 300              # queued commands survive ~5 min of disconnection, then EXPIRE
TERMINAL_STATUSES = {"EXECUTED", "REJECTED", "FAILED", "EXPIRED"}
RESULT_STATUSES = {"ACCEPTED", "EXECUTED", "REJECTED", "FAILED"}  # what an Agent may report

commands = []              # append-only [ command record ] (see the spec object below)
commands_by_id = {}        # {id: record} — uuid id is the dedup key (no duplicate execution)
# Terminal results reported for a command this process has no record of — an ORPHANED
# HISTORICAL RESULT. Because commands_by_id is append-only within a process (never pruned)
# an unknown id can only mean the command belonged to a PRIOR process (the in-memory queue
# was lost on restart) or is simply bogus: either way no current command exists to apply it
# to, and none ever will. We archive it here for audit (keyed by command id, so a replayed
# orphan is recorded once, not once per retry) instead of failing the report — see
# process_command_result. { command_id: {first_seen, last_seen, count, last_status,
# last_reason, vehicle_id} }.
orphaned_command_results = {}


def known_vehicle_ids():
    """Canonical ids the backend recognises: configured vehicles + any that have reported."""
    return set(fleet_vehicle_ids())


def _command_event(cmd, *, severity, message, source):
    """Record a command lifecycle change as a first-class event, carrying the structured
    command detail (id, type, source, stage, normalized verification outcome) so the Events
    page can show it as an expandable detail view — not just a message string."""
    ver = cmd.get("verification") or {}
    _append_event(
        severity=severity, message=message, etype="command", source=source,
        vehicle_id=cmd["vehicle_id"], vehicle=cmd["vehicle"],
        detail={
            "command_id": cmd["id"],
            "command_type": cmd["type"],
            "command_source": cmd.get("source"),
            "stage": cmd["status"],
            "outcome": ver.get("outcome"),
            "verified": ver.get("verified"),
            "expected": ver.get("expected"),
            "observed": ver.get("observed"),
            "reason": ver.get("reason") or cmd.get("reason"),
        },
    )


def _canonical_set_home_params(raw_params):
    """SET_HOME always means "Scout's own current position at execution time" — a
    browser-supplied lat/lng is a snapshot from whenever the operator last had a fix and
    can be stale, or simply wrong, by the time the Local Agent actually calls Scout Flask.
    The only authoritative field is mode:"current_position"; Scout chooses and verifies
    its own position. Any lat/lng the caller supplied is kept, nested and clearly
    separate, ONLY as non-authoritative audit metadata (what the operator's UI showed at
    click time) — never as a target coordinate."""
    canonical = {"mode": "current_position"}
    raw = raw_params if isinstance(raw_params, dict) else {}
    lat, lng = raw.get("lat"), raw.get("lng")
    if lat is not None and lng is not None:
        canonical["requested_position"] = {"lat": lat, "lng": lng}
    return canonical


# --- mission-contract-v1 (Scout-owned mission upload contract) -------------------
#
# THE DIVISION OF OWNERSHIP, which every function below depends on:
#   • The OPERATOR supplies ROUTE waypoints only — the survey legs, nothing else.
#   • SCOUT owns Pixhawk sequence 0 / Home. The operator never sends a seq-0 item and
#     never numbers its waypoints; Scout prepends Home when it writes to the FC.
#   • Therefore the item count the Pixhawk holds after a successful upload is
#     route waypoint count + 1 — N route legs plus Scout's Home at seq 0.
# The old operator-side {seq, command, lat, lng, alt} shape encoded the opposite
# assumption (operator owns sequencing and MAVLink command codes) and is gone.
MISSION_CONTRACT_VERSION = mission_contract.CONTRACT_VERSION

# Route CONTENT verification is live. The Operator backend is the authoritative expected-hash
# calculator (mission_contract.route_content_hash) — there is no frontend copy that could
# drift from it. See mission_contract.py for the canonicalization Scout defines.

# MISSION_CLEAR is queueable: Scout ships POST /agent/clear_mission through the queued
# LOCAL_AGENT MISSION_CLEAR command, with a result contract carrying the independent empty
# read-back a clear must be judged by.
MISSION_CLEAR_SUPPORTED = True
MISSION_CLEAR_UNSUPPORTED_REASON = None

# The two empty states ArduPilot legitimately reports after a clear. Scout supports BOTH,
# so a verified clear must accept both: some stacks wipe every item, others retain Home at
# seq 0. What matters is that no ROUTE remains — hence route count 0 is required while
# Pixhawk item count is deliberately NOT forced to 0.
MISSION_EMPTY_REPRESENTATIONS = ("NO_ITEMS", "HOME_ONLY")

# Maximum route waypoints in ONE upload. THE single limit — canonical_mission_upload_params
# enforces it, and both callers (POST /api/missions/preview and POST /api/commands) go
# through that function, so preview can never accept a route the upload would refuse. A
# preview that succeeds for a mission the command endpoint rejects is the specific defect
# this shared constant exists to prevent.
#
# PROVENANCE — SCOUT OWNS THIS NUMBER. mission-contract-v1 defines and enforces
# MAX_ROUTE_WAYPOINTS = 200, and Scout refuses an oversized mission with the structured
# error {code: "MISSION_TOO_LARGE", maximum_route_waypoints, observed_route_waypoints}.
# The value here MIRRORS Scout's; it is not an independent Operator judgement, and it must
# not be tuned locally. If Scout changes its limit, this constant follows — changing it
# here alone would put the two systems back out of agreement, which is exactly what the
# earlier Operator-chosen placeholder risked.
#
# WHY THE OPERATOR STILL VALIDATES, given Scout enforces it too: rejecting locally means an
# oversized mission fails at preview — before anything is queued and before a single byte
# reaches the vehicle — instead of being transmitted, refused, and reported back as a failed
# command. Scout remains the AUTHORITY (its refusal is final and its structured error is
# what the operator is shown); this check is a fail-fast mirror of that authority, never a
# second opinion about what the limit is.
MAX_ROUTE_WAYPOINTS = 200
MAX_ROUTE_WAYPOINTS_SOURCE = "scout-contract"

# Scout's structured error code for an oversized mission (mission-contract-v1). Rendered
# with Scout's OWN numbers — see mission_error_text.
MISSION_TOO_LARGE_CODE = "MISSION_TOO_LARGE"

# Keys a caller may NOT supply at the top level of a mission request. `expected_*` are
# DERIVED by this backend, which is the single authoritative calculator — accepting a
# browser-supplied expected_route_content_hash would let the client choose the value its
# own upload is later "verified" against, which is not a verification at all. Refused
# loudly rather than ignored, so an attempt to inject one is visible instead of silent.
MISSION_DERIVED_ONLY_FIELDS = (
    "expected_route_content_hash",
    "expected_route_waypoint_count",
    "expected_pixhawk_item_count",
)

# Fields a caller may send per route waypoint. Anything else is refused rather than
# silently dropped: Scout does not yet accept arbitrary MAVLink items, so previewing a
# `command`/`frame`/`altitude` the flight controller will never receive would be showing
# the operator a mission that is not the mission (UI-honesty: never render what the
# backend cannot back up).
MISSION_WAYPOINT_FIELDS = {"latitude", "longitude", "loiter_time_s"}
# Rejected with a specific message naming why, so the operator can fix the file rather
# than guess. These are exactly the fields Scout would discard on receipt.
MISSION_UNSUPPORTED_FIELDS = {
    "command": "MAVLink command codes are Scout-owned — Scout writes every route item as "
               "a NAV_WAYPOINT. Remove `command`.",
    "frame": "MAVLink frames are Scout-owned. Remove `frame`.",
    "altitude": "Altitude is not part of mission-contract-v1 (surface vessel). Remove `altitude`.",
    "alt": "Altitude is not part of mission-contract-v1 (surface vessel). Remove `alt`.",
    "seq": "Sequence numbers are Scout-owned — Scout owns seq 0 / Home and numbers the "
           "route itself. Remove `seq`.",
}


def mission_error_text(error):
    """Operator-facing text for a STRUCTURED Scout mission error, or None when Scout sent
    nothing structured enough to render.

    Returning None is the important half: the caller then falls back to its own generic
    wording. This function NEVER invents a explanation, and never pads Scout's structured
    error with a guess — when Scout says MISSION_TOO_LARGE and states both numbers, those
    numbers ARE the explanation, and appending "the mission may not have been uploaded"
    style filler would bury a precise, actionable fact under boilerplate.

    A MISSION_TOO_LARGE whose numbers are missing renders what Scout actually sent and says
    the counts were not reported — it does not substitute MAX_ROUTE_WAYPOINTS for the
    maximum Scout omitted, because that would present an Operator constant as Scout's word.
    """
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    if code == MISSION_TOO_LARGE_CODE:
        maximum = error.get("maximum_route_waypoints")
        observed = error.get("observed_route_waypoints")
        if maximum is not None and observed is not None:
            return (f"Mission too large — Scout accepts at most {maximum} route waypoints "
                    f"under {MISSION_CONTRACT_VERSION}; this route submitted {observed}.")
        return ("Mission too large — Scout refused the route as MISSION_TOO_LARGE but did "
                "not report both counts.")
    return error.get("message") or (str(code) if code else None)


class MissionContractError(ValueError):
    """A mission upload request that does not satisfy mission-contract-v1. Carries the
    per-waypoint messages so the UI can list every problem at once, not just the first."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def _mission_number(raw):
    """A finite float from a JSON number, or None. Strings are NOT coerced: a quoted
    coordinate means the producing tool lost its typing, and silently accepting it is how
    a wrong mission gets uploaded looking correct."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    f = float(raw)
    return f if math.isfinite(f) else None


def canonical_mission_upload_params(raw_params):
    """Validate + canonicalize a MISSION_UPLOAD request body into the stored params.

    Input (mission-contract-v1, route waypoints ONLY — no seq-0 Home):
        {"contract_version": "mission-contract-v1",
         "waypoints": [{"latitude": .., "longitude": .., "loiter_time_s": 0}, ...]}

    Returns the canonical params carrying BOTH counts the read-back is verified against:
        expected_route_waypoint_count = N        (what the operator supplied)
        expected_pixhawk_item_count   = N + 1    (N route legs + Scout's Home at seq 0)

    Raises MissionContractError listing every problem. Never partially accepts: a mission
    with one bad waypoint is refused whole, because uploading "most of" a route to a
    flight controller is worse than uploading none of it."""
    raw = raw_params if isinstance(raw_params, dict) else {}
    errors = []

    version = raw.get("contract_version")
    if version is not None and str(version) != MISSION_CONTRACT_VERSION:
        errors.append(
            f"Unsupported contract_version {version!r} — this station speaks "
            f"{MISSION_CONTRACT_VERSION}.")

    # A client may not supply what the backend derives — above all the expected hash, which
    # is the value the upload is later verified against. See MISSION_DERIVED_ONLY_FIELDS.
    for field in MISSION_DERIVED_ONLY_FIELDS:
        if field in raw:
            errors.append(
                f"`{field}` is derived by the operator backend and may not be supplied by "
                f"the caller — remove it.")

    items = raw.get("waypoints")
    if not isinstance(items, list):
        errors.append("Missing `waypoints` — expected a list of route waypoints.")
        raise MissionContractError(errors)
    if not items:
        errors.append("Mission contains no route waypoints.")
        raise MissionContractError(errors)
    # Checked BEFORE per-waypoint validation: a 5000-waypoint route should report the one
    # actionable problem, not 5000 derived ones.
    if len(items) > MAX_ROUTE_WAYPOINTS:
        # Worded as Scout's limit because it IS Scout's — the operator should not be told a
        # local policy refused their mission when the flight system's contract did.
        errors.append(
            f"Route has {len(items)} route waypoints — mission-contract-v1 accepts at most "
            f"{MAX_ROUTE_WAYPOINTS} (Scout-enforced). Refused here before transmission; "
            f"Scout would reject it as {MISSION_TOO_LARGE_CODE}. "
            f"Split the mission into shorter routes.")
        raise MissionContractError(errors)

    waypoints = []
    for i, item in enumerate(items):
        # 1-based position: this is operator-facing text about a route, not an array index,
        # and it is deliberately NOT a `seq` (the operator does not own sequencing).
        pos = i + 1
        if not isinstance(item, dict):
            errors.append(f"Route waypoint {pos} is not an object.")
            continue
        for field, why in MISSION_UNSUPPORTED_FIELDS.items():
            if field in item:
                errors.append(f"Route waypoint {pos}: {why}")
        unknown = set(item) - MISSION_WAYPOINT_FIELDS - set(MISSION_UNSUPPORTED_FIELDS)
        if unknown:
            errors.append(
                f"Route waypoint {pos}: unsupported field(s) {', '.join(sorted(unknown))} — "
                f"mission-contract-v1 accepts only {', '.join(sorted(MISSION_WAYPOINT_FIELDS))}.")

        lat = _mission_number(item.get("latitude"))
        lng = _mission_number(item.get("longitude"))
        if lat is None or abs(lat) > 90:
            errors.append(f"Route waypoint {pos}: `latitude` must be a number in [-90, 90].")
        if lng is None or abs(lng) > 180:
            errors.append(f"Route waypoint {pos}: `longitude` must be a number in [-180, 180].")
        loiter = item.get("loiter_time_s", 0)
        loiter_f = _mission_number(loiter)
        if loiter_f is None or loiter_f < 0:
            errors.append(f"Route waypoint {pos}: `loiter_time_s` must be a number >= 0.")
        if lat is None or lng is None or loiter_f is None:
            continue
        waypoints.append({"latitude": lat, "longitude": lng, "loiter_time_s": loiter_f})

    if errors:
        raise MissionContractError(errors)

    n = len(waypoints)
    return {
        "contract_version": MISSION_CONTRACT_VERSION,
        "waypoints": waypoints,
        "expected_route_waypoint_count": n,
        # Scout prepends Home at seq 0 — the operator never sends it, but the read-back
        # must show it, so N+1 is what a correct upload leaves on the flight controller.
        "expected_pixhawk_item_count": n + 1,
        # The content axis: the only one that proves the route on the FC is the route the
        # operator approved. Both counts can be correct for a route with two waypoints
        # swapped. Computed HERE, in the backend, as the single authoritative calculator —
        # the browser renders this string and never recomputes it.
        "expected_route_content_hash": mission_contract.route_content_hash(waypoints),
    }


def make_command(*, vid, ctype, params, created_by, comm_state, now, source=None):
    """Build + store a QUEUED command record (the spec command object).

    `source` is deliberately still a parameter even though the ONLY caller today
    (create_command, the browser-facing endpoint) hard-codes "OPERATOR". It is the seam
    for the trusted internal paths the contract anticipates — a LOCAL_AGENT- or
    MISSION_AGENT-authored command must be created by such a function, never by request
    JSON. Until one exists, normalize_source's non-OPERATOR branches are reachable only
    from tests; that is scaffolding, not dead code."""
    cmd = {
        "id": str(uuid.uuid4()),
        "vehicle_id": vid,
        "vehicle": name_of(vid),
        "type": ctype,
        "params": params or {},
        "status": "QUEUED",
        # Normalized command source (OPERATOR / LOCAL_AGENT / MISSION_AGENT) — forwarded to
        # Scout in agent_command_view and preserved in every record/history view.
        "source": normalize_source(source or created_by),
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=COMMAND_TTL_S)).isoformat(),
        "created_by": created_by or "operator",
        "requested_comm_state": comm_state,   # operator-side link state at creation
        "claimed_at": None,                   # set when the Agent first fetches it (SENT)
        "completed_at": None,                 # set on any terminal status
        "result": None,                       # Agent-reported result payload/string
        "reason": None,                       # rejection/failure/expiry reason
        "warning": None,                      # e.g. queued while PARTITIONED
        # Normalized command lifecycle + verification (the spec fields). `verification` is
        # the ONE type-agnostic outcome the UI reads (verified/outcome/expected/observed/
        # reason); `lifecycle` is the ordered stage list (backend queue stages merged with
        # any Scout-provided result.lifecycle); `error` is the structured Scout error.
        # `scout_lifecycle` retains Scout's own array verbatim. All refreshed on mutation.
        "error": None,
        "scout_lifecycle": None,
        "verification": None,
        "lifecycle": None,
    }
    commands.append(cmd)
    commands_by_id[cmd["id"]] = cmd
    _refresh_command_derived(cmd)
    return cmd


def expire_commands(now=None):
    """Flip any non-terminal command past its TTL to EXPIRED (backend-owned, once)."""
    now = now or datetime.now(timezone.utc)
    for cmd in commands:
        if cmd["status"] in TERMINAL_STATUSES:
            continue
        try:
            deadline = datetime.fromisoformat(cmd["expires_at"])
        except (TypeError, ValueError):
            continue
        if now >= deadline:
            cmd["status"] = "EXPIRED"
            cmd["completed_at"] = now.isoformat()
            cmd["reason"] = cmd["reason"] or "Expired before delivery/execution"
            _refresh_command_derived(cmd)
            _command_event(cmd, severity="warning",
                           message=f"Command {cmd['type']} expired",
                           source="operator-backend")


def apply_command_result(cmd, new_status, result, reason, now):
    """Apply a Local-Agent-reported result. Idempotent: a result on an already-terminal
    command is ignored (the uuid id prevents duplicate execution). Returns True if applied."""
    if cmd["status"] in TERMINAL_STATUSES:
        return False
    cmd["status"] = new_status
    cmd["result"] = result
    cmd["reason"] = reason
    if new_status in TERMINAL_STATUSES:
        cmd["completed_at"] = now.isoformat()
    return True


# Field spellings a Local Agent might use for the command id / lifecycle status /
# detail / reason, so the result receiver accepts either endpoint's payload shape.
_RESULT_ID_KEYS = ("command_id", "id", "cmd_id", "commandId", "uuid")
_RESULT_STATUS_KEYS = ("status", "result_status", "outcome", "state")
_RESULT_REASON_KEYS = ("reason", "error", "message", "detail")
_RESULT_VEHICLE_KEYS = ("vehicle_id", "usv_id", "vehicleId", "vid")
# A Local Agent may report TIMEOUT/ACK/DONE/OK — normalize to the lifecycle vocabulary
# the queue owns (RESULT_STATUSES) so no valid outcome is dropped as "invalid".
_RESULT_STATUS_ALIASES = {
    "TIMEOUT": "FAILED", "TIMED_OUT": "FAILED", "TIMEDOUT": "FAILED",
    "ERROR": "FAILED", "FAIL": "FAILED", "FAILURE": "FAILED",
    "REJECT": "REJECTED", "DENIED": "REJECTED",
    "ACK": "ACCEPTED", "ACKNOWLEDGED": "ACCEPTED",
    "DONE": "EXECUTED", "COMPLETE": "EXECUTED", "COMPLETED": "EXECUTED",
    "SUCCESS": "EXECUTED", "OK": "EXECUTED", "EXECUTE": "EXECUTED",
}


def _pick(d: dict, keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def normalize_result_status(raw):
    """Map a reported status onto the queue's RESULT_STATUSES vocabulary, or None."""
    s = str(raw or "").upper().strip()
    s = _RESULT_STATUS_ALIASES.get(s, s)
    return s if s in RESULT_STATUSES else None


def _annotate_set_home_result(cmd):
    """Classify a SET_HOME command's own nested Scout result for IMMEDIATE operator
    feedback ONLY (the pending flash resolving / a toast) — this is NEVER the permanent
    Home-verification source. That is home_block(), driven solely by Scout's
    continuously-reported payload.agent.home_status; nothing here writes to it.

    Command-protocol status EXECUTED means only "the Local Agent successfully called
    Scout Flask" — it does NOT mean Set Home succeeded, so a bare EXECUTED is never
    enough. A result only counts as a verified Set Home when ALL of:
      - result.accepted is True (not just truthy/absent)
      - result.verified is True
      - result.home_position has a usable latitude/longitude (never the requested
        params — those prove what was ASKED for, not what Pixhawk actually returned)
      - result.verification_distance_m is present and within tolerance (belt-and-
        suspenders against a Local Agent that reports verified:true with a bogus
        distance; Scout's own tolerance already gates `verified`, this just refuses to
        compound trust in a suspicious number)
    Anything else sets cmd["home_result"] = "failed" and replaces cmd["reason"] with
    Scout's actual error (never inventing one), even though cmd["status"] stays
    EXECUTED — the transport succeeded; Set Home itself did not."""
    if cmd["type"] != "SET_HOME" or cmd["status"] != "EXECUTED":
        return
    result = cmd.get("result") if isinstance(cmd.get("result"), dict) else {}
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    home_position = result.get("home_position") if isinstance(result.get("home_position"), dict) else None
    lat = _mission_coord(home_position.get("latitude"), home_position.get("lat")) if home_position else None
    lng = _mission_coord(home_position.get("longitude"), home_position.get("lng")) if home_position else None
    distance_m = result.get("verification_distance_m")

    failure_reason = None
    if result.get("accepted") is not True:
        failure_reason = error.get("message") or error.get("code") or "Set Home was not accepted by Scout."
    elif result.get("verified") is not True:
        failure_reason = error.get("message") or error.get("code") or "Home was not verified by Scout's read-back."
    elif lat is None or lng is None:
        failure_reason = "Scout reported verified without a usable home_position."
    elif distance_m is None:
        failure_reason = "Scout did not report a verification distance."
    elif distance_m > HOME_VERIFY_TOLERANCE_M:
        failure_reason = (f"Home read back {distance_m} m from the requested point — "
                           f"outside the {HOME_VERIFY_TOLERANCE_M} m tolerance.")

    if failure_reason:
        cmd["home_result"] = "failed"
        cmd["reason"] = failure_reason
    else:
        cmd["home_result"] = "verified"


def _mode_is_rtl(mode) -> bool:
    """True when a reported flight mode is RTL, canonicalizing first so a numeric
    custom_mode (11) and its numeric string ("11") count exactly like the name "RTL".
    SMART_RTL (12) is deliberately NOT RTL — it is a distinct recovery mode."""
    return normalize_rover_mode(mode) == "RTL"


def _annotate_rtl_result(cmd):
    """Classify an RTL command's own nested Scout result for the operator UI — the RTL
    twin of _annotate_set_home_result. This is NEVER the vehicle's live mode (that comes
    from telemetry); it only says whether THIS RTL attempt actually put the Pixhawk into
    RTL, so the command row never renders a green 'confirmed' on transport success alone.

    Command-protocol status EXECUTED means only 'the Local Agent completed the attempt'.
    An RTL is a verified success ONLY when ALL of:
      - result.accepted is True   (MAVLink accepted the mode change)
      - result.verified is True   (Scout read the mode back)
      - result.observed_mode canonicalizes to RTL (the read-back mode IS RTL — the ack
        alone is not trusted; a vehicle can ack then revert)
    Crucially, observed_mode is canonicalized (normalize_rover_mode) BEFORE the compare, so
    a numeric custom_mode 11 (or the string "11") is recognised as RTL and never rejected
    as 'mode 11, not RTL' — that false-negative was the whole reason for this change. The
    raw value is retained as cmd['observed_raw']. When Scout's accepted+verified evidence
    is present but the read-back is an UNRECOGNISED representation (or absent), the outcome
    is 'unverified' — EXECUTED but not independently confirmed, and NOT a vehicle rejection:
    Scout's verified=true is authoritative and must not be overturned by a representation
    gap (see verify_mode_match / MODE_VERIFY_*). A genuinely DIFFERENT known mode (RTL vs
    MANUAL) is still a real 'failed'.

    Anything else — including the legacy optimistic {"status": "Returning home"} shape,
    which carries no accepted/verified flags — sets cmd['rtl_result'] = 'failed' and
    replaces cmd['reason'] with Scout's real error / observed mode (never invented),
    even though cmd['status'] stays EXECUTED (the transport succeeded; RTL did not)."""
    if cmd["type"] != "RTL" or cmd["status"] != "EXECUTED":
        return
    result = cmd.get("result") if isinstance(cmd.get("result"), dict) else {}
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    accepted = result.get("accepted")
    verified = result.get("verified")
    observed_raw = result.get("observed_mode")
    previous_raw = result.get("previous_mode")
    err_msg = error.get("message") or error.get("code")

    # Canonical forms for the classification/wording; the untouched raw value is preserved.
    observed = normalize_rover_mode(observed_raw)
    previous = normalize_rover_mode(previous_raw)
    if observed_raw is not None:
        cmd["observed_raw"] = observed_raw

    failure_reason = None
    unverifiable = None
    if accepted is not True:
        # No explicit acceptance — covers a rejected mode change AND the legacy
        # {"status":"Returning home"} HTTP-200 shape (no accepted flag at all). Never a
        # verified RTL just because a route returned 200.
        failure_reason = err_msg or "MAVLink rejected the RTL mode change."
    elif verified is not True:
        if err_msg:
            failure_reason = err_msg
        elif observed is not None and observed != "RTL":
            failure_reason = (f"Mode reverted from RTL to {observed}"
                              if previous == "RTL" else f"Pixhawk remained in {observed}")
        else:
            failure_reason = "RTL verification timed out."
    else:
        # accepted AND verified — Scout's authoritative vehicle-side evidence that RTL took.
        # Compare canonically: 11 / "11" / "RTL" all confirm; only a different KNOWN mode
        # overturns it, and an unrecognised representation is 'unverified', never a failure.
        verdict = verify_mode_match("RTL", observed_raw)
        if verdict == MODE_VERIFY_VERIFIED:
            pass  # confirmed — the read-back canonicalizes to RTL
        elif verdict == MODE_VERIFY_FAILED:
            failure_reason = f"Pixhawk reported {observed}, not RTL."
        elif verdict == MODE_VERIFY_UNKNOWN:
            unverifiable = (f"Scout verified RTL but reported an unrecognised observed mode "
                            f"{observed_raw!r}; the RTL representation could not be confirmed.")
        else:  # MODE_VERIFY_UNVERIFIED — verified with no observed mode reported at all
            unverifiable = "Scout confirmed RTL without reporting the observed flight mode."

    if failure_reason:
        cmd["rtl_result"] = "failed"
        cmd["reason"] = failure_reason
    elif unverifiable:
        # Respect Scout's authority (not a failure) but refuse a silent green success.
        cmd["rtl_result"] = "unverified"
        cmd["reason"] = unverifiable
    else:
        cmd["rtl_result"] = "confirmed"


def _annotate_mission_upload_result(cmd):
    """Classify a MISSION_UPLOAD / MISSION_CLEAR command's own nested Scout result — the
    mission twin of _annotate_set_home_result / _annotate_rtl_result. Status EXECUTED means
    only 'the Local Agent completed the attempt against Scout'; a mission write is a verified
    success ONLY when Scout accepted it AND read it back AND the read-back matches what the
    operator asked for. Never 'successful' just because the file reached Scout.

    MISSION_UPLOAD (mission-contract-v1) is verified when ALL of:
      - result.accepted is True   (Scout accepted the mission)
      - result.uploaded is True   (checked only when Scout provides the field)
      - result.verified is True   (Scout re-downloaded the mission from the FC)
      - observed route waypoint count == expected_route_waypoint_count (N)
      - observed Pixhawk item count   == expected_pixhawk_item_count   (N + 1, incl. Home)
      - observed route content hash   == expected_route_content_hash
    Under mission-contract-v1 every axis is REQUIRED, and a missing expected or observed
    value is an explicit unverifiable FAILURE — never a silent pass, and never a
    count-only success dressed up as content-verified. That distinction is the whole point
    of the hash: the counts cannot detect two swapped waypoints or a wrong coordinate, so
    "counts matched, hash absent" is exactly the case that must not render as verified.
    Records without a v1 contract_version (pre-contract history) keep the older
    count-only path — compatibility that does not weaken the v1 proof, because it cannot
    apply to a v1 upload. The two counts are deliberately BOTH checked: N alone would miss
    a Scout that dropped Home, N+1 alone would miss one that dropped a route leg.

    MISSION_CLEAR is verified when ALL of:
      - result.accepted is True
      - result.cleared is True
      - result.verified is True
      - observed_route_waypoint_count == 0
      - empty_representation is NO_ITEMS or HOME_ONLY
    The Pixhawk item count is deliberately NOT required to be 0: Scout supports both
    ArduPilot empty representations, and a retained Home at seq 0 (item count 1) is a
    correctly cleared mission. What must be zero is the ROUTE.

    Anything else sets cmd['mission_result'] = 'failed' and replaces cmd['reason'] with
    Scout's real error (never invented), even though cmd['status'] stays EXECUTED."""
    if cmd["type"] not in ("MISSION_UPLOAD", "MISSION_CLEAR") or cmd["status"] != "EXECUTED":
        return
    result = cmd.get("result") if isinstance(cmd.get("result"), dict) else {}
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    params = cmd.get("params") if isinstance(cmd.get("params"), dict) else {}
    # A structured Scout error (e.g. MISSION_TOO_LARGE with its two counts) is rendered from
    # Scout's OWN fields, and takes precedence over every generic fallback below.
    err_msg = mission_error_text(error)

    exp_route = params.get("expected_route_waypoint_count")
    exp_items = params.get("expected_pixhawk_item_count")
    exp_hash = params.get("expected_route_content_hash")
    obs_route = _first_present(result.get("observed_route_waypoint_count"),
                               result.get("route_waypoint_count"))
    obs_items = _first_present(result.get("observed_pixhawk_item_count"),
                               result.get("pixhawk_item_count"), result.get("observed_count"),
                               result.get("count"), result.get("mission_count"))
    # Scout's ROUTE hash (items 1…N), never its full-mission hash — a full-mission hash
    # includes Home, which the operator never sent and therefore cannot have hashed.
    obs_hash = _first_present(result.get("observed_route_content_hash"),
                              result.get("route_content_hash"))

    # Is this command governed by the v1 contract? Only v1 records carry the mandatory
    # content-hash axis; anything older cannot be held to a proof it never produced.
    is_v1 = str(params.get("contract_version")) == MISSION_CONTRACT_VERSION

    failure = None
    if result.get("accepted") is not True:
        failure = err_msg or f"{cmd['type']} was not accepted by Scout."
    elif cmd["type"] == "MISSION_UPLOAD" and result.get("uploaded") is False:
        # Only an explicit False fails — Scout may omit `uploaded` entirely, and absence is
        # not evidence of a failed write.
        failure = err_msg or "Scout accepted the mission but did not upload it."
    elif cmd["type"] == "MISSION_CLEAR" and result.get("cleared") is not True:
        failure = err_msg or "Scout did not report the mission as cleared."
    elif result.get("verified") is not True:
        failure = err_msg or "Mission was not verified by Scout's read-back."
    elif cmd["type"] == "MISSION_CLEAR":
        empty_repr = result.get("empty_representation")
        if obs_route is None:
            failure = ("Clear could not be verified — Scout reported no "
                       "observed_route_waypoint_count.")
        elif int(obs_route) != 0:
            failure = f"Mission still holds {obs_route} route waypoints after clear."
        elif empty_repr not in MISSION_EMPTY_REPRESENTATIONS:
            # Home-only (item count 1) and no-items (0) are BOTH correct; anything else is
            # an empty state neither side defined, so it is not a proof of a clear.
            failure = (f"Clear could not be verified — unrecognised empty representation "
                       f"{empty_repr!r}.")
    else:  # MISSION_UPLOAD
        if exp_route is not None and obs_route is not None and int(obs_route) != int(exp_route):
            failure = (f"Pixhawk holds {obs_route} route waypoints after upload — "
                       f"expected {exp_route}.")
        elif exp_items is not None and obs_items is not None and int(obs_items) != int(exp_items):
            # The parenthetical is only added when the route count is actually known — a
            # record predating this contract would otherwise render "None route waypoints"
            # straight into the operator-facing failure reason.
            breakdown = f" ({exp_route} route waypoints + Home)" if exp_route is not None else ""
            failure = (f"Pixhawk holds {obs_items} items after upload — "
                       f"expected {exp_items}{breakdown}.")
        elif exp_hash and obs_hash and str(exp_hash) != str(obs_hash):
            failure = "Uploaded route does not match the read-back — the on-FC route differs."
        elif is_v1 and (obs_route is None or obs_items is None):
            missing = "route waypoint" if obs_route is None else "Pixhawk item"
            failure = (f"Upload could not be verified — Scout reported no observed "
                       f"{missing} count.")
        elif is_v1 and not exp_hash:
            # Unreachable for a command built by canonical_mission_upload_params; caught
            # anyway so a future path that forgets the hash fails loudly rather than
            # quietly downgrading to count-only verification.
            failure = ("Route content could not be verified — no expected route content "
                       "hash was computed for this upload.")
        elif is_v1 and not obs_hash:
            failure = ("Route content could not be verified — Scout reported no "
                       "observed_route_content_hash. The counts matched, but matching "
                       "counts do not prove the route's contents.")

    if failure:
        cmd["mission_result"] = "failed"
        cmd["reason"] = failure
    else:
        cmd["mission_result"] = "verified"


def _annotate_generic_verification(cmd):
    """Surface a reason for any OTHER command type (mode/arming/mission-pause) whose Scout
    result carries an explicit verified:false. SET_HOME/RTL/MISSION_* have their own
    classifiers (skipped here). A command with no `verified` field in its result is a plain
    EXECUTED success and is left untouched (AUTO/MANUAL/LOITER/… back-compat)."""
    if cmd["type"] in ("SET_HOME", "RTL", "MISSION_UPLOAD", "MISSION_CLEAR"):
        return
    if cmd["status"] != "EXECUTED":
        return
    result = cmd.get("result") if isinstance(cmd.get("result"), dict) else {}
    if result.get("verified") is False:
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        expected, observed = _extract_expected_observed(cmd, result)
        cmd["reason"] = (cmd.get("reason") or error.get("message") or error.get("code")
                         or (f"Vehicle reported {observed}, expected {expected}."
                             if observed and expected else
                             f"{cmd['type']} was not verified by the vehicle."))


def _extract_expected_observed(cmd, result):
    """(expected, observed) display values for a command's verification, tolerant of the
    field spellings Scout may use. Mission commands summarise as waypoint counts."""
    expected = _first_present(
        result.get("expected_mode"), result.get("requested_mode"),
        result.get("expected_state"), result.get("expected"))
    observed = _first_present(
        result.get("observed_mode"), result.get("observed_state"),
        result.get("observed"), result.get("mode"))
    if cmd["type"] in ("MISSION_UPLOAD", "MISSION_CLEAR"):
        # Summarised as PIXHAWK ITEM counts (N+1, Home included) — that is the number the
        # read-back actually reports, so expected and observed are the same kind of thing.
        params = cmd.get("params") if isinstance(cmd.get("params"), dict) else {}
        ec = params.get("expected_pixhawk_item_count")
        oc = _first_present(result.get("observed_pixhawk_item_count"),
                            result.get("pixhawk_item_count"), result.get("observed_count"),
                            result.get("count"), result.get("mission_count"))
        expected = f"{ec} Pixhawk items" if ec is not None else expected
        observed = f"{oc} Pixhawk items" if oc is not None else observed
        return expected, observed
    # Mode/arming commands: display the CANONICAL Rover mode name so a bare numeric
    # custom_mode (11) never reaches the operator where it means a named mode (RTL). An
    # unrecognised value (or a non-mode state like ARMED) falls through to its raw form.
    expected = normalize_rover_mode(expected) or expected
    observed = normalize_rover_mode(observed) or observed
    return expected, observed


def _outcome_label(status, verified):
    """Normalized terminal outcome vocabulary for the UI:
      PENDING  — not terminal yet
      VERIFIED — EXECUTED and the vehicle action was confirmed
      EXECUTED — EXECUTED with no separate verification (plain success)
      FAILED   — EXECUTED but verification failed, OR a FAILED status
      REJECTED / EXPIRED — the corresponding terminal status."""
    if status not in TERMINAL_STATUSES:
        return "PENDING"
    if status == "EXECUTED":
        if verified is True:
            return "VERIFIED"
        if verified is False:
            return "FAILED"
        return "EXECUTED"
    return status


def build_command_verification(cmd):
    """The ONE normalized, type-agnostic verification outcome every UI reads, so no page
    re-implements per-type logic. Never optimistic: an EXECUTED whose per-type verification
    did not pass reads verified:false; an unknown/older record with no verification for a
    command that HAS one (RTL/SET_HOME/MISSION_*) is conservatively unverified, never green.

      verified: True  → confirmed vehicle action
                False → EXECUTED transport but the vehicle action was NOT confirmed
                None  → not applicable (not terminal-executed, or a type with no separate
                        verification reporting nothing) — the plain status stands."""
    status = cmd["status"]
    ctype = cmd["type"]
    result = cmd.get("result") if isinstance(cmd.get("result"), dict) else {}
    error = cmd.get("error") if isinstance(cmd.get("error"), dict) else (
        result.get("error") if isinstance(result.get("error"), dict) else {})
    expected, observed = _extract_expected_observed(cmd, result)
    # The vehicle's observed mode/state exactly as Scout reported it (numeric custom_mode,
    # numeric string or name), before canonicalization — kept so the raw evidence behind a
    # normalized display value is never lost (e.g. observed "RTL" with observed_raw 11).
    observed_raw = cmd.get("observed_raw")
    if observed_raw is None:
        observed_raw = _first_present(
            result.get("observed_mode"), result.get("observed_state"),
            result.get("observed"), result.get("mode"))

    verified = None
    if status == "EXECUTED":
        if ctype == "SET_HOME":
            verified = cmd.get("home_result") == "verified"
        elif ctype == "RTL":
            # Tri-state: 'confirmed' → True (green), 'unverified' → None (EXECUTED, not a
            # failure — Scout's authority respected despite an unconfirmable representation),
            # anything else (incl. 'failed' / missing) → False.
            rr = cmd.get("rtl_result")
            verified = True if rr == "confirmed" else (None if rr == "unverified" else False)
        elif ctype in ("MISSION_UPLOAD", "MISSION_CLEAR"):
            verified = cmd.get("mission_result") == "verified"
        elif "verified" in result:
            verified = result.get("verified") is True
        # else: no verification reported for a plain mode/arming command → None (success).

    reason = None
    if verified is False or status in ("REJECTED", "FAILED", "EXPIRED"):
        reason = cmd.get("reason") or error.get("message") or error.get("code")
    elif verified is None and status == "EXECUTED" and cmd.get("rtl_result") == "unverified":
        # Surface WHY an EXECUTED RTL is not a confirmed success (representation gap), so the
        # UI can explain the EXECUTED_UNVERIFIED state rather than showing a bare green.
        reason = cmd.get("reason")

    return {
        "verified": verified,
        "outcome": _outcome_label(status, verified),
        "expected": expected,
        "observed": observed,
        "observed_raw": observed_raw,
        "reason": reason,
    }


def command_lifecycle(cmd):
    """Ordered lifecycle stages with timestamps: the backend-owned queue stages merged with
    any Scout-provided fine-grained array (result.lifecycle, retained on cmd['scout_lifecycle'])
    — one list the UI renders as the command's progression, newest logic last."""
    stages = []
    if cmd.get("created_at"):
        stages.append({"stage": "QUEUED", "ts": cmd["created_at"], "by": "operator-backend"})
    if cmd.get("claimed_at"):
        stages.append({"stage": "SENT", "ts": cmd["claimed_at"], "by": "operator-backend"})
    for st in (cmd.get("scout_lifecycle") or []):
        if isinstance(st, dict):
            stages.append({
                "stage": str(st.get("stage") or st.get("status") or st.get("name") or "?").upper(),
                "ts": st.get("ts") or st.get("timestamp") or st.get("time"),
                "by": st.get("by") or "scout",
            })
        elif st:
            stages.append({"stage": str(st).upper(), "ts": None, "by": "scout"})
    if cmd.get("completed_at"):
        stages.append({"stage": cmd["status"], "ts": cmd["completed_at"], "by": "scout"})
    return stages


def _refresh_command_derived(cmd):
    """Recompute the normalized verification + lifecycle fields on a record after any state
    change. Called from every mutation site so a serialized record is always self-consistent."""
    cmd["verification"] = build_command_verification(cmd)
    cmd["lifecycle"] = command_lifecycle(cmd)


def _archive_orphaned_result(command_id, status, reason, vehicle_id, now):
    """Record a terminal result reported for a command this process has no record of — an
    ORPHANED HISTORICAL RESULT (the queue was lost on restart, or the id is bogus). We do
    NOT apply it to any current command and never fabricate one; we keep it as a low-severity
    audit trail so an operator can see Scout reported *something* whose command is gone,
    without it masquerading as a live command failure. Idempotent by command id: a replayed
    orphan (Scout draining its buffer) updates the seen count but emits the audit event only
    ONCE, so a retry storm never floods the event log."""
    key = str(command_id)
    rec = orphaned_command_results.get(key)
    # Known-vehicles-only (parse_vehicle_id semantics): an orphan result naming a vehicle
    # the station has never heard of records no vehicle rather than inventing one.
    vid = parse_vehicle_id(vehicle_id) if vehicle_id is not None else None
    if vid == -1:
        vid = None
    if rec is None:
        orphaned_command_results[key] = {
            "command_id": key, "first_seen": now.isoformat(), "last_seen": now.isoformat(),
            "count": 1, "last_status": status, "last_reason": reason, "vehicle_id": vid,
        }
        # First sighting only → one audit event. Severity INFO: this is an audit note about a
        # command that no longer exists, NOT a failure of any command the operator is watching.
        _append_event(
            severity="info", etype="command",
            source=(vehicle_slug(vid) if vid is not None else "operator-backend"),
            vehicle_id=vid, vehicle=(name_of(vid) if vid is not None else None),
            message=(f"Orphaned command result archived — reported {status or 'result'} for "
                     f"unknown command {key} (no current command; not applied)"),
            detail={
                "command_id": key, "command_type": None, "command_source": "LOCAL_AGENT",
                "stage": "ORPHANED", "outcome": "ORPHANED", "verified": None,
                "expected": None, "observed": None,
                "reason": reason or "command not found in this backend (queue lost on restart or unknown id)",
            },
        )
    else:
        rec["last_seen"] = now.isoformat()
        rec["count"] += 1
        rec["last_status"] = status
        rec["last_reason"] = reason
        if vid is not None:
            rec["vehicle_id"] = vid


def process_command_result(command_id, raw_status, result, reason, now, vehicle_id=None):
    """Look up a command and apply a Local-Agent-reported result. Single source of truth
    shared by BOTH result endpoints (POST /agent/command_result and the id-in-path
    /api/commands/{id}/result). Idempotent by the uuid command id: a duplicate/replayed
    result on an already-terminal command is a no-op (applied:false) — no double history
    row, no double execution. Returns a per-item dict; never raises.

    An unknown command id is an ORPHANED HISTORICAL RESULT (found:false, orphaned:true),
    NOT a malformed request: the item is well-formed, it just names a command this process
    no longer holds. It is archived for audit (idempotently) and never mutates any current
    command; the /agent/command_result endpoint answers it with a TERMINAL 2xx so Scout
    stops retrying, while a *missing* id (no id at all → orphaned:false) stays a 400."""
    if not command_id:
        return {"command_id": command_id, "found": False, "applied": False,
                "orphaned": False, "error": "missing command id"}
    cmd = commands_by_id.get(str(command_id))
    if cmd is None:
        # Unknown id: no current command exists (and none ever will — the store is append-only
        # within a process, so this id belonged to a prior process or is bogus). Archive it as
        # an orphaned audit event and acknowledge terminally — a 404 here just makes Scout
        # retry an unresolvable result forever. Current command state is untouched.
        _archive_orphaned_result(command_id, normalize_result_status(raw_status) or raw_status,
                                 reason, vehicle_id, now)
        return {"command_id": command_id, "found": False, "applied": False,
                "orphaned": True, "error": "unknown command id"}
    new_status = normalize_result_status(raw_status)
    if new_status is None:
        return {"command_id": command_id, "found": True, "applied": False,
                "error": "invalid result status", "status": raw_status,
                "allowed": sorted(RESULT_STATUSES)}
    applied = apply_command_result(cmd, new_status, result, reason, now)
    if applied:
        # Retain Scout's own structured error + fine-grained lifecycle array verbatim, so
        # the normalized verification/lifecycle fields can be rebuilt from them.
        result_obj = cmd.get("result") if isinstance(cmd.get("result"), dict) else {}
        if isinstance(result_obj.get("error"), dict):
            cmd["error"] = result_obj["error"]
        if isinstance(result_obj.get("lifecycle"), list):
            cmd["scout_lifecycle"] = result_obj["lifecycle"]
        # Command-specific result classifiers run BEFORE the event message so it carries
        # the real reason. Each inspects only its own command type's nested result and
        # may replace cmd['reason']; a bare EXECUTED with no per-type verification (AUTO/
        # MANUAL/LOITER/ARM/…) is left untouched and reads as a normal success.
        _annotate_set_home_result(cmd)
        _annotate_rtl_result(cmd)
        _annotate_mission_upload_result(cmd)
        _annotate_generic_verification(cmd)
        # Follow a finalized-survey upload onto its immutable original mission record: a
        # verified read-back marks the original mission VERIFIED; a failure records FAILED
        # while preserving the record. No-op for uploads not created via /api/missions/finalize.
        _sync_mission_record_status(cmd)
        _refresh_command_derived(cmd)
        # One normalized outcome drives severity + wording (SET_HOME/RTL/MISSION_UPLOAD and
        # any command Scout reports verified:false for).
        verify_failed = cmd["verification"]["verified"] is False
        sev = "warning" if new_status in ("REJECTED", "FAILED") or verify_failed else "info"
        # An outer EXECUTED that the command's own verification did not confirm (Set Home
        # not read back, RTL not actually entered) is reported as a verification failure,
        # not "executed" — the transport succeeded, the vehicle action did not.
        msg = (f"Command {cmd['type']} verification failed" if verify_failed
               else f"Command {cmd['type']} {new_status.lower()}")
        if cmd.get("reason"):
            msg = f"{msg} — {cmd['reason']}"
        _command_event(cmd, severity=sev, message=msg, source=vehicle_slug(cmd["vehicle_id"]))
    return {"command_id": command_id, "found": True, "applied": applied,
            "status": new_status, "command": cmd}


def _unwrap_envelope(item):
    """Unwrap ONE canonical Local Agent message envelope — { message_type:
    "command_result", schema_version, source, target, timestamp, payload:{command_id,
    status, result, ...} } — down to its `payload`, which is the actual result dict.
    Detected ONLY when message_type is EXACTLY "command_result" and payload is an object;
    anything else (including the legacy flat {command_id, status, ...} shape, which has
    no message_type/payload at all) passes through completely unchanged — the two forms
    are never conflated. Applied once per item, never recursively, so a payload that
    itself happens to carry message_type/payload keys is not unwrapped again."""
    if (isinstance(item, dict) and item.get("message_type") == "command_result"
            and isinstance(item.get("payload"), dict)):
        return item["payload"]
    return item


def _result_items(body):
    """Normalize a command-result request body into a list of result dicts. Accepts a
    single object, a bare JSON list, or {results:[...]}/{command_results:[...]} — so a
    single ack and a flushed backlog both work through one endpoint. Each candidate item
    is then run through _unwrap_envelope, so any of the three shapes may carry either the
    legacy flat result or a full canonical message envelope — including a backlog flush
    that is itself a list of envelopes rather than flat payloads."""
    if isinstance(body, list):
        items = [x for x in body if isinstance(x, dict)]
    elif isinstance(body, dict):
        items = None
        for key in ("results", "command_results", "items", "acks"):
            if isinstance(body.get(key), list):
                items = [x for x in body[key] if isinstance(x, dict)]
                break
        if items is None:
            items = [body]
    else:
        items = []
    return [_unwrap_envelope(it) for it in items]


@app.post("/api/commands")
async def create_command(request: Request):
    """Create a command for a vehicle (Operator UI / curl). Validates the type and
    vehicle, then gates on the OPERATOR-side comm-state:
      CONNECTED   → queued immediately
      PARTITIONED → queued with a warning
      DISCONNECTED→ 409 needs_confirmation, unless body has confirm:true (then queued;
                    it survives until next contact or the TTL).
    High-risk arming (ARM/DISARM) ALWAYS needs confirm:true regardless of comm-state
    (409 otherwise); ARM additionally carries a caution warning in its record.
    Never marks the command executed — that is the Local Agent's result only.
    Body: { vehicle_id, type, params?, created_by?, confirm? }"""
    now = datetime.now(timezone.utc)
    expire_commands(now)
    body = await request.json()

    ctype = str(body.get("type") or "").upper()
    if ctype not in COMMAND_TYPES:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "unknown command type",
            "type": body.get("type"), "allowed": sorted(COMMAND_TYPES)})

    vid = parse_vehicle_id(body.get("vehicle_id"))
    if vid not in known_vehicle_ids():
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "unknown vehicle", "vehicle_id": body.get("vehicle_id")})

    comm_state = comms_state_by_id.get(vid, "UNKNOWN")
    confirm = bool(body.get("confirm"))

    # High-risk arming: ARM/DISARM always require confirm:true, whatever the link state.
    if ctype in CONFIRM_REQUIRED_TYPES and not confirm:
        return JSONResponse(status_code=409, content={
            "ok": False, "needs_confirmation": True, "type": ctype,
            "high_risk": ctype == "ARM",
            "message": f"{ctype} affects the motors and requires explicit confirmation. "
                       "Resend with confirm:true."})
    # Disconnected: queue-until-contact still needs a confirmation for any command.
    if comm_state == "DISCONNECTED" and not confirm:
        return JSONResponse(status_code=409, content={
            "ok": False, "needs_confirmation": True, "comm_state": comm_state,
            "message": "Vehicle is DISCONNECTED — command will queue until next contact. "
                       "Resend with confirm:true to queue it."})

    params = body.get("params")
    if ctype == "SET_HOME":
        params = _canonical_set_home_params(params)
    elif ctype == "MISSION_UPLOAD":
        try:
            params = canonical_mission_upload_params(params)
        except MissionContractError as exc:
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "mission_contract_violation",
                "contract_version": MISSION_CONTRACT_VERSION,
                "message": "Mission does not satisfy " + MISSION_CONTRACT_VERSION + ".",
                "errors": exc.errors})

    # `source` is SERVER-OWNED, never taken from the request body. Every command created
    # through this browser-facing endpoint is authored by the human at this station, so it
    # is OPERATOR by construction. A LOCAL_AGENT / MISSION_AGENT record must be created by
    # a separate trusted backend function, not by arbitrary request JSON — otherwise any
    # caller could mint a record attributing its own command to the autonomy, and the
    # provenance trail that the thesis's authority analysis rests on would be worthless.
    # A body-supplied `source` is ignored (not an error): the field is simply not the
    # client's to set, and rejecting would break callers that send a redundant "OPERATOR".
    cmd = make_command(vid=vid, ctype=ctype, params=params,
                       created_by=body.get("created_by"), comm_state=comm_state, now=now,
                       source="OPERATOR")

    # Accumulate any warnings (risk + link state) into the record; a warning implies caution.
    warnings = []
    if ctype in RISK_WARNING:
        warnings.append(RISK_WARNING[ctype])
    if comm_state == "PARTITIONED":
        warnings.append("Queued while communication is partitioned — delivery may be delayed.")
    elif comm_state == "DISCONNECTED":
        warnings.append("Queued while disconnected — will deliver on next contact.")
    if warnings:
        cmd["warning"] = " ".join(warnings)
    severity = "caution" if warnings else "info"
    _command_event(cmd, severity=severity,
                   message=f"Command {ctype} created ({comm_state})",
                   source="operator-backend")
    return {"ok": True, "command": cmd}


@app.get("/api/commands/capabilities")
def command_capabilities():
    """Which command types this station can actually deliver TODAY, and why not when it
    cannot. The UI reads this to disable a button with the real backend reason instead of
    hard-coding its own guess about Scout — one source of truth for "is this supported",
    so a button can never be enabled for a command the backend would refuse.
    Returns { contract_version, commands: {TYPE: {supported, reason}} }."""
    caps = {t: {"supported": True, "reason": None} for t in sorted(COMMAND_TYPES)}
    caps["MISSION_CLEAR"] = {
        "supported": MISSION_CLEAR_SUPPORTED,
        "reason": None if MISSION_CLEAR_SUPPORTED else MISSION_CLEAR_UNSUPPORTED_REASON,
    }
    return {
        "ok": True,
        "contract_version": MISSION_CONTRACT_VERSION,
        "commands": caps,
        # Published so the UI can state the real limit instead of hard-coding a second copy
        # of it. `source` is here because the number's PROVENANCE is operator-facing:
        # "scout-contract" means mission-contract-v1 defines and enforces this limit and the
        # Operator mirrors it, which is what the UI tells the operator. A locally chosen
        # limit would have to say so instead — the field exists so the two can never be
        # confused for one another.
        "max_route_waypoints": MAX_ROUTE_WAYPOINTS,
        "max_route_waypoints_source": MAX_ROUTE_WAYPOINTS_SOURCE,
    }


@app.post("/api/missions/preview")
async def mission_preview(request: Request):
    """Canonicalize a route WITHOUT queueing anything, so the Mission page can show the
    operator the expected route content hash before they approve the upload.

    This endpoint exists because the browser deliberately has no hash calculator: the
    backend is the single authoritative one (see mission_contract.py). Previewing the
    counts locally but the hash not at all would ask the operator to approve a mission
    whose identity they cannot see, and computing it in JavaScript would create the second
    implementation the contract exists to avoid.

    READ-ONLY BY CONSTRUCTION, and this is load-bearing: it creates no command, appends no
    event, and touches no authority or vehicle state. The only thing it calls is
    canonical_mission_upload_params — the SAME function POST /api/commands calls — so the
    validation, the error list, the waypoint limit (MAX_ROUTE_WAYPOINTS) and the derived
    params are identical by construction rather than by two implementations agreeing. A
    preview that accepted a route the upload would refuse would show the operator a hash for
    a mission they cannot actually send; the shared function is what makes that impossible.
    Nothing in the request body reaches the derived fields — an `expected_route_content_hash`
    supplied by the browser is REFUSED, never echoed back as if it had been computed."""
    try:
        body = await request.json()
    except Exception:
        # A malformed body is the caller's error, not a 500. Same shape as the contract
        # violation below so the UI has one error path to render.
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "mission_contract_violation",
            "contract_version": MISSION_CONTRACT_VERSION,
            "message": "Request body is not valid JSON.",
            "errors": ["Request body is not valid JSON."]})
    try:
        params = canonical_mission_upload_params(body)
    except MissionContractError as exc:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "mission_contract_violation",
            "contract_version": MISSION_CONTRACT_VERSION,
            "message": "Mission does not satisfy " + MISSION_CONTRACT_VERSION + ".",
            "errors": exc.errors})
    return {"ok": True, "params": params}


# ── Survey mission planning (Plan page) ──────────────────────────────────────────────
# Planning is OPERATOR-owned: the Plan page constructs a side-scan survey and hands Scout a
# finalized, validated mission package. Generation/validation are deterministic and run
# without a live Scout — they are pure geometry over the operator's inputs (see planning.py,
# which ports Scout's tested lawnmower + return-path generators). Upload is deliberately NOT
# a new endpoint here: a generated route is route waypoints, uploaded through the SAME
# POST /api/commands MISSION_UPLOAD path as a pasted mission, so there is exactly one
# mission-upload framework, one contract and one read-back verification — never a second.
#
# Drafts are editable planning documents (NOT uploaded missions), persisted as JSON files in
# the existing lightweight style (the backend has no database; see SYSTEM_INFORMATION_MODEL).
PLANNING_DRAFTS_DIR = BASE_DIR / "planning_drafts"


def _planning_unavailable_response():
    """Honest 503 when the geometry stack is not installed — never a 500, and never a
    fabricated empty route. UI-honesty applied to a whole feature."""
    return JSONResponse(status_code=503, content={
        "ok": False, "error": "planning_unavailable",
        "message": ("Survey planning is unavailable in this backend — it requires shapely, "
                    "pyproj and numpy, which are not installed. Install them to enable the "
                    "Plan page's route generation."),
        "detail": planning.PLANNING_IMPORT_ERROR})


@app.post("/api/planning/generate")
async def planning_generate(request: Request):
    """Generate a segmented side-scan survey route from the operator's planning inputs.

    Body: { boundary (GeoJSON Polygon or ring), shoreline_clearance_m, no_go_zones[],
    lane_spacing_m, primary_angle_deg, dual_pass, secondary_angle_deg, home?,
    transit_waypoints[]?, survey_speed_mps? }. Returns segments (typed geometry for the map),
    route_waypoints (flat mission-contract route), metrics, intersections and warnings.
    Deterministic and read-only: no command is created, no vehicle state is touched."""
    if not planning.PLANNING_AVAILABLE:
        return _planning_unavailable_response()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "bad_request", "message": "Request body is not valid JSON."})
    try:
        result = planning.generate_survey(body, max_route_waypoints=MAX_ROUTE_WAYPOINTS)
    except ValueError as exc:
        # A planning-input problem the operator can fix (empty inset, no coverage, bad
        # geometry) — a 400 with the specific reason(s), not a 500.
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "planning_input_invalid", "message": str(exc),
            "errors": str(exc).split("; ")})
    result["max_route_waypoints"] = MAX_ROUTE_WAYPOINTS
    return result


@app.post("/api/planning/validate")
async def planning_validate(request: Request):
    """Deterministically validate a generated plan before upload. Body carries the planning
    inputs plus the generated `route_waypoints` and `segments`. Returns {ok, errors,
    warnings, checks}. Errors block upload; warnings do not. Read-only."""
    if not planning.PLANNING_AVAILABLE:
        return _planning_unavailable_response()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "bad_request", "message": "Request body is not valid JSON."})
    result = planning.validate_plan(body, max_route_waypoints=MAX_ROUTE_WAYPOINTS)
    return result


# ── Fleet survey planning (Plan page — Fleet Mission mode) ────────────────────────────
# A fleet plan divides ONE shared survey area between two or more registered USVs. Generation
# is a deterministic layer ABOVE the single-vehicle planner (fleet_planning.py): shared
# geometry → survey lines → contiguous home-aware allocation → one INDEPENDENT child mission
# per vehicle. Each child mission is an ordinary operator-survey-plan-v1 package, so upload is
# NOT a new endpoint here — the frontend orchestrates one POST /api/missions/finalize per
# vehicle, reusing the unchanged canonicalise/hash/read-back-verify path per vehicle. This is
# static pre-deployment deconfliction, never runtime collision avoidance.


@app.post("/api/planning/fleet/generate")
async def fleet_generate(request: Request):
    """Generate a fleet plan (child missions + allocation + fleet validation) from shared
    survey geometry and the selected vehicles. Body: { boundary, shoreline_clearance_m,
    no_go_zones[], lane_spacing_m, primary_angle_deg, dual_pass, secondary_angle_deg,
    minimum_fleet_separation_m, balance_metric, vehicles:[{vehicle_id, vehicle_name, colour,
    home, survey_speed_mps}], manual_assignments? }. Deterministic and read-only: no command
    is created, no vehicle state is touched."""
    if not planning.PLANNING_AVAILABLE:
        return _planning_unavailable_response()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "bad_request", "message": "Request body is not valid JSON."})
    try:
        result = fleet_planning.generate_fleet(body, max_route_waypoints=MAX_ROUTE_WAYPOINTS)
    except fleet_planning.FleetPlanError as exc:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "fleet_input_invalid", "message": str(exc),
            "errors": str(exc).split("; ")})
    except planning.DisconnectedNavigableError as exc:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "fleet_navigable_disconnected", "message": str(exc)})
    except planning.ConnectorError as exc:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "fleet_connector_failed", "message": str(exc)})
    except ValueError as exc:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "fleet_input_invalid", "message": str(exc)})
    return result


@app.post("/api/planning/fleet/validate")
async def fleet_validate(request: Request):
    """Re-run fleet conflict validation on a supplied fleet plan (blocking errors, warnings,
    informational metrics). Body is a fleet plan (fleet_planning.generate_fleet output).
    Read-only."""
    if not planning.PLANNING_AVAILABLE:
        return _planning_unavailable_response()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "bad_request", "message": "Request body is not valid JSON."})
    return fleet_planning.validate_fleet(body)


def _draft_path(draft_id: str):
    """Resolve a draft id to its JSON file, refusing any id that escapes the drafts dir."""
    name = Path(str(draft_id)).name  # strip any path components
    if not name or name != str(draft_id):
        return None
    return PLANNING_DRAFTS_DIR / f"{name}.json"


def _load_draft(draft_id: str):
    path = _draft_path(draft_id)
    if path is None or not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _draft_summary(draft: dict):
    return {
        "id": draft.get("id"),
        "name": draft.get("name"),
        "created_at": draft.get("created_at"),
        "updated_at": draft.get("updated_at"),
        "vehicle_id": draft.get("vehicle_id"),
        "waypoint_count": ((draft.get("plan") or {}).get("metrics") or {}).get("waypoint_count"),
        "state": draft.get("state"),
    }


@app.post("/api/planning/drafts")
async def create_draft(request: Request):
    """Save a new planning draft (an editable planning document, never an uploaded mission).
    Body: { name?, vehicle_id?, state?, plan: {...geometry, params, generated route...} }.
    Returns the stored draft with its assigned id."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    now = datetime.now(timezone.utc).isoformat()
    draft_id = uuid.uuid4().hex[:12]
    draft = {
        "id": draft_id,
        "name": str(body.get("name") or f"Draft {draft_id}"),
        "vehicle_id": body.get("vehicle_id"),
        "state": body.get("state"),
        "created_at": now,
        "updated_at": now,
        "plan": body.get("plan") or {},
    }
    PLANNING_DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(PLANNING_DRAFTS_DIR / f"{draft_id}.json", "w", encoding="utf-8") as f:
        json.dump(draft, f, indent=2)
    return {"ok": True, "draft": draft}


@app.get("/api/planning/drafts")
def list_drafts():
    """List saved planning drafts (summaries only), newest first."""
    drafts = []
    if PLANNING_DRAFTS_DIR.exists():
        for path in PLANNING_DRAFTS_DIR.glob("*.json"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    drafts.append(_draft_summary(json.load(f)))
            except Exception:
                continue
    drafts.sort(key=lambda d: d.get("updated_at") or "", reverse=True)
    return {"ok": True, "drafts": drafts}


@app.get("/api/planning/drafts/{draft_id}")
def get_draft(draft_id: str):
    draft = _load_draft(draft_id)
    if draft is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "not_found"})
    return {"ok": True, "draft": draft}


@app.put("/api/planning/drafts/{draft_id}")
async def update_draft(draft_id: str, request: Request):
    """Overwrite an existing draft's editable fields, preserving id + created_at."""
    existing = _load_draft(draft_id)
    if existing is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "not_found"})
    try:
        body = await request.json()
    except Exception:
        body = {}
    existing["name"] = str(body.get("name") or existing.get("name"))
    if "vehicle_id" in body:
        existing["vehicle_id"] = body.get("vehicle_id")
    if "state" in body:
        existing["state"] = body.get("state")
    if "plan" in body:
        existing["plan"] = body.get("plan") or {}
    existing["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(PLANNING_DRAFTS_DIR / f"{existing['id']}.json", "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)
    return {"ok": True, "draft": existing}


@app.delete("/api/planning/drafts/{draft_id}")
def delete_draft(draft_id: str):
    path = _draft_path(draft_id)
    if path is None or not path.exists():
        return JSONResponse(status_code=404, content={"ok": False, "error": "not_found"})
    try:
        path.unlink()
    except Exception as exc:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})
    return {"ok": True, "deleted": draft_id}


# ── Immutable original mission records (Plan finalization; future agent replanning) ───────
# When a finalized survey is uploaded, the Operator MUST NOT lose the richer geometry it
# flattened for the Pixhawk. The MISSION_UPLOAD command still carries ONLY the proven
# mission-contract-v1 route (unchanged) — but alongside it we store ONE immutable original
# mission record (revision 0) retaining the operator inputs, segmented purposes, navigable
# geometry, no-go zones, the original execution order and the canonical route hash. This is
# the substrate a Local Agent revision will later derive from; NONE of that replanning is
# implemented here (see PART 6/7 of the task) — only the record and read-only APIs.
#
# The record's route_hash is the SAME canonical hash the upload command is verified against
# (mission_contract.route_content_hash) — never a second identity.
#
# These two stores are DURABLE (see the snapshot below): an approved, verified mission and
# which mission is active per vehicle must survive an operator-station restart, because they
# cannot be reconstructed — only a fresh plan → upload → verified read-back creates one, and
# losing the record silently drops replanning readiness for a vehicle that is still flying
# that route. `mission_id_by_command` deliberately stays in memory: it links a record to a
# COMMAND, and the command queue does not survive a restart, so a persisted link would point
# at nothing. After a restart the snapshot's recorded upload_status is the surviving truth.
original_missions = {}            # {mission_id: record}   (durable)
mission_id_by_command = {}        # {upload_command_id: mission_id}  (in-memory: see above)
active_original_by_vehicle = {}   # {vehicle_id: mission_id}  (latest finalized revision 0; durable)

# ── Durable mission store ─────────────────────────────────────────────────────────────
# ONE atomic JSON snapshot of exactly the two stores above. Deliberately narrow: nothing
# transient is persisted — no telemetry, no commands, no event log, no readiness/Pixhawk
# read-back caches, no Scout package responses. Those are live evidence, and a restored copy
# of live evidence would be a fabricated observation; they are re-read from the vehicle and
# from Scout on the next poll.
#
# Writes are atomic (temp file + os.replace) so a crash mid-write can never leave a half
# written snapshot behind, and the store FAILS CLOSED on load: a corrupt, unreadable or
# incompatible file is logged and the station starts with an EMPTY mission store rather than
# a partially-loaded one. Half a mission store is worse than none — it would present a
# mission whose geometry may not be what the vehicle is carrying.
#
# WHERE THE STORE LIVES — and the interlock that keeps a TEST out of it.
# ---------------------------------------------------------------------
# The production store holds the operator's APPROVED, verified missions and which mission each
# vehicle is actually flying. It is safety-relevant state that cannot be reconstructed, so the
# only processes permitted to write it are real backends.
#
# This was not a hypothetical. A single test module that restored the real `_save_mission_store`
# and then ran a publish wrote its OWN isolated in-memory store — one seeded fixture mission —
# straight over `runtime_data/mission_store.json`. The station then insisted, correctly, that
# the Agent package did not match the approved mission, while Scout and the Pixhawk were flying
# something else entirely. Isolation had appeared to work only because a FULL `unittest discover`
# run happens to import `tests/test_planning.py` first, and that module redirects the path at
# import time; running one module on its own — exactly what the per-feature docs instruct — left
# the production path live.
#
# Isolation that depends on module import ORDER is not isolation. So the path is resolved once,
# here, in the module that owns the store, and a test process can never resolve it to production:
#
#   1. OPERATOR_RUNTIME_DIR — an explicit override, for a deployment that keeps its runtime
#      state elsewhere and for a test that wants a directory it chose itself.
#   2. a test runner in the process — `unittest` / `pytest` imported before main. A per-process
#      temporary directory is used instead, and the substitution is logged loudly. No test can
#      opt back in, and a NEW test file inherits the guarantee without doing anything.
#   3. otherwise — the real `runtime_data/` beside this module.
RUNTIME_DIR_ENV = "OPERATOR_RUNTIME_DIR"
PRODUCTION_RUNTIME_DIR = BASE_DIR / "runtime_data"


def _test_runner_in_process():
    """True when this interpreter was started by a test runner. Checked against sys.modules
    rather than a flag a test must remember to set: `python -m unittest ...` has imported
    `unittest` long before main.py, and a real `uvicorn main:app` has not."""
    import sys
    return "unittest" in sys.modules or "pytest" in sys.modules


def _resolve_runtime_dir():
    """(directory, reason) for this process's runtime state. See the note above."""
    override = (os.environ.get(RUNTIME_DIR_ENV) or "").strip()
    if override:
        return Path(override), "env"
    if _test_runner_in_process():
        import tempfile
        return Path(tempfile.mkdtemp(prefix="operator-runtime-test-")), "test"
    return PRODUCTION_RUNTIME_DIR, "production"


MISSION_STORE_DIR, MISSION_STORE_SOURCE = _resolve_runtime_dir()
MISSION_STORE_PATH = MISSION_STORE_DIR / "mission_store.json"
MISSION_STORE_VERSION = 1

if MISSION_STORE_SOURCE == "test":
    print(f"[MISSION STORE] test runner detected — the production store "
          f"({PRODUCTION_RUNTIME_DIR / 'mission_store.json'}) is NOT reachable from this "
          f"process; using {MISSION_STORE_PATH}")


def is_production_mission_store():
    """True only when this process resolved the REAL production store. The regression guard in
    tests/test_mission_store_isolation.py asserts this is false under every test runner."""
    return Path(MISSION_STORE_PATH).resolve() == (PRODUCTION_RUNTIME_DIR / "mission_store.json").resolve()


def _mission_store_snapshot():
    """The exact JSON-able payload written to disk."""
    return {
        "version": MISSION_STORE_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "original_missions": original_missions,
        # JSON object keys are strings; vehicle ids are ints, so they are stringified here
        # and parsed back through parse_vehicle_id on load.
        "active_original_by_vehicle": {str(k): v for k, v in active_original_by_vehicle.items()},
    }


def _active_missions_log_text():
    """`usv-2=msn-… (VERIFIED, package SYNCED)` for every vehicle with an active mission.

    Logged after EVERY persistence update, not only at startup, so the terminal shows which
    mission each vehicle is actually on and whether its Scout package is owed. A single startup
    line could only ever describe the store as it was restored."""
    parts = []
    for v, m in sorted(active_original_by_vehicle.items()):
        rec = original_missions.get(m) or {}
        sync = rec.get("package_sync_state") or "NOT SYNCED"
        parts.append(f"{vehicle_slug(v)}={m} ({rec.get('upload_status')}, package {sync})")
    return ", ".join(parts) or "none"


def _save_mission_store():
    """Persist the mission store atomically. Never raises: a station that cannot write its
    snapshot must keep operating on its in-memory truth, loudly, rather than fail a mission
    operation because of a disk problem."""
    try:
        MISSION_STORE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = MISSION_STORE_PATH.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_mission_store_snapshot(), fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, MISSION_STORE_PATH)          # atomic on Windows and POSIX
        print(f"[MISSION STORE] saved {len(original_missions)} record(s) to "
              f"{MISSION_STORE_PATH}; active: {_active_missions_log_text()}")
        return True
    except Exception as exc:
        print(f"[MISSION STORE] could not write {MISSION_STORE_PATH}: {exc} - "
              "continuing with the in-memory store; this restart will lose it")
        return False


def _validate_mission_store(data):
    """(missions, active) from a snapshot, or raise ValueError. Validates the WHOLE file
    before anything is adopted, so a single bad record rejects the file instead of loading
    the rest — partial mission state is exactly what must never be presented as approved."""
    if not isinstance(data, dict):
        raise ValueError("snapshot root is not an object")
    version = data.get("version")
    if version != MISSION_STORE_VERSION:
        raise ValueError(f"unsupported snapshot version {version!r} "
                         f"(this build reads {MISSION_STORE_VERSION})")
    raw_missions = data.get("original_missions")
    raw_active = data.get("active_original_by_vehicle")
    if not isinstance(raw_missions, dict) or not isinstance(raw_active, dict):
        raise ValueError("original_missions / active_original_by_vehicle are not objects")

    missions = {}
    for mid, rec in raw_missions.items():
        if not isinstance(mid, str) or not mid:
            raise ValueError(f"invalid mission id {mid!r}")
        if not isinstance(rec, dict):
            raise ValueError(f"record for {mid} is not an object")
        if rec.get("mission_id") != mid:
            raise ValueError(f"record for {mid} carries mission_id {rec.get('mission_id')!r}")
        if parse_vehicle_id(rec.get("vehicle_id")) is None:
            raise ValueError(f"record {mid} has unresolvable vehicle_id {rec.get('vehicle_id')!r}")
        if not isinstance(rec.get("route_waypoints"), list) or not rec["route_waypoints"]:
            raise ValueError(f"record {mid} has no route waypoints")
        if not isinstance(rec.get("route_hash"), str) or not rec["route_hash"]:
            raise ValueError(f"record {mid} has no route hash")
        if rec.get("upload_status") not in MISSION_UPLOAD_STATUSES:
            raise ValueError(f"record {mid} has upload_status {rec.get('upload_status')!r}")
        # The record is immutable geometry: its stored hash must still be the canonical hash
        # of its own waypoints. A record that fails this was altered on disk.
        recomputed = mission_contract.route_content_hash(rec["route_waypoints"])
        if recomputed != rec["route_hash"]:
            raise ValueError(f"record {mid} route hash does not match its waypoints "
                             f"(stored {rec['route_hash']}, recomputed {recomputed})")
        rec = dict(rec)
        rec["vehicle_id"] = parse_vehicle_id(rec.get("vehicle_id"))
        missions[mid] = rec

    active = {}
    for raw_vid, mid in raw_active.items():
        vid = parse_vehicle_id(raw_vid)
        if vid is None:
            raise ValueError(f"active mission is keyed by unresolvable vehicle {raw_vid!r}")
        if mid not in missions:
            raise ValueError(f"vehicle {raw_vid} points at unknown mission {mid!r}")
        if missions[mid]["vehicle_id"] != vid:
            raise ValueError(f"mission {mid} is active for {raw_vid} but belongs to "
                             f"{missions[mid]['vehicle_id']}")
        active[vid] = mid
    return missions, active


def _load_mission_store():
    """Restore the mission store at startup. Returns a short status string for the log.

    Fails CLOSED: any read/parse/validation problem leaves BOTH stores empty and is logged;
    the snapshot file is left on disk untouched for inspection rather than being repaired or
    partially adopted."""
    original_missions.clear()
    active_original_by_vehicle.clear()
    if not MISSION_STORE_PATH.exists():
        return "no snapshot - starting with an empty mission store"
    try:
        with open(MISSION_STORE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        missions, active = _validate_mission_store(data)
    except Exception as exc:
        print(f"[MISSION STORE] REFUSED {MISSION_STORE_PATH}: {exc}")
        print("[MISSION STORE] starting with an EMPTY mission store; no record was partially "
              "loaded. Re-plan and re-upload to restore an approved mission.")
        return f"refused ({exc})"
    original_missions.update(missions)
    active_original_by_vehicle.update(active)
    if not missions:
        return "snapshot held no records"
    # Includes each active mission's upload_status and package_sync_state, so a restored
    # PACKAGE_SYNC_REQUIRED is visible in the startup log rather than only after the next poll.
    return (f"restored {len(missions)} mission record(s) from {MISSION_STORE_PATH}; "
            f"active: {_active_missions_log_text()}")

# The command lifecycle → mission upload_status projection. QUEUED/SENT are both "queued"
# from the mission's point of view; a verified read-back is the only VERIFIED.
MISSION_UPLOAD_STATUSES = ("QUEUED", "ACCEPTED", "VERIFIED", "FAILED")


def _mission_upload_status_for(cmd):
    """Project a MISSION_UPLOAD command's current lifecycle onto the mission record's
    upload_status. VERIFIED requires the read-back verification (_annotate_mission_upload_
    result set mission_result:'verified'), never mere transport success."""
    status = cmd.get("status")
    if status in ("QUEUED", "SENT"):
        return "QUEUED"
    if status == "ACCEPTED":
        return "ACCEPTED"
    if status == "EXECUTED":
        return "VERIFIED" if cmd.get("mission_result") == "verified" else "FAILED"
    if status in ("REJECTED", "FAILED", "EXPIRED"):
        return "FAILED"
    return "QUEUED"


def _sync_mission_record_status(cmd):
    """Follow a linked MISSION_UPLOAD command's lifecycle onto its immutable mission record.
    A failed upload keeps the record (upload_status FAILED, plan preserved); a verified
    read-back marks the ORIGINAL mission VERIFIED. The record itself is never mutated beyond
    its upload_status/verified_at — it is immutable geometry."""
    if cmd.get("type") != "MISSION_UPLOAD":
        return
    mid = mission_id_by_command.get(cmd["id"])
    rec = original_missions.get(mid) if mid else None
    if rec is None:
        return
    new_status = _mission_upload_status_for(cmd)
    previous = rec.get("upload_status")
    rec["upload_status"] = new_status
    if new_status == "VERIFIED" and not rec.get("verified_at"):
        rec["verified_at"] = datetime.now(timezone.utc).isoformat()
    if new_status == "FAILED":
        rec["upload_failure_reason"] = cmd.get("reason")
    if new_status != previous:
        # Every upload-status change, verification included, is durable: a restart must not
        # demote a VERIFIED mission back to whatever the last snapshot happened to hold.
        _save_mission_store()


def _new_mission_record(vehicle_id, package, command):
    """Build + store the immutable revision-0 original mission record for a finalized upload.
    `package` is the operator-survey-plan-v1 generate output; `command` is the QUEUED
    MISSION_UPLOAD record. The record's route_hash is the command's authoritative expected
    hash (the two are the same route by construction)."""
    now = datetime.now(timezone.utc).isoformat()
    mission_id = "msn-" + uuid.uuid4().hex[:12]
    params = command.get("params") or {}
    rec = {
        "mission_id": mission_id,
        "mission_revision": 0,
        "parent_revision_id": None,
        "vehicle_id": vehicle_id,
        "mission_package_version": package.get("mission_package_version", planning.MISSION_PACKAGE_VERSION),
        "route_contract_version": MISSION_CONTRACT_VERSION,
        # Authoritative canonical hash — the SAME one the upload read-back is verified against.
        "route_hash": params.get("expected_route_content_hash"),
        "input_revision": package.get("input_revision"),
        "planning_inputs": package.get("planning_inputs") or {},
        "navigable_geometry": package.get("navigable_boundary")
                              or (package.get("planning_inputs") or {}).get("navigable_boundary"),
        "no_go_zones": (package.get("planning_inputs") or {}).get("no_go_zones") or [],
        "segments": package.get("segments") or [],
        "original_execution_order": package.get("original_execution_order") or [],
        "route_waypoints": params.get("waypoints") or package.get("route_waypoints") or [],
        "metrics": package.get("metrics") or {},
        "created_at": now,
        "upload_command_id": command["id"],
        "upload_status": "QUEUED",
        "verified_at": None,
        "upload_failure_reason": None,
        "immutable": True,
        # Reserved for later Local Agent revisions (documented flow; NOT implemented now):
        # original revision 0 → Scout obstacle event → Local Agent revision 1 → revised flat
        # route uploaded + verified → Operator stores revision 1 linked to this original.
        "revision_reason": None,
        "blocked_segments": None,
        "derived_from_route_hash": None,
    }
    original_missions[mission_id] = rec
    mission_id_by_command[command["id"]] = mission_id
    active_original_by_vehicle[vehicle_id] = mission_id   # replaces this vehicle's active one
    _save_mission_store()      # finalization + active-original replacement, in one snapshot
    return rec


@app.post("/api/missions/finalize")
async def finalize_mission(request: Request):
    """Finalize a generated survey: store the immutable original mission record (revision 0)
    AND create the verified MISSION_UPLOAD command, in one call. The command carries only the
    proven mission-contract-v1 route (unchanged upload path); the record retains the richer
    package so the geometry/segmentation survives flattening.

    Body: { vehicle_id, mission_package (operator-survey-plan-v1 generate output), confirm }.
    MISSION_UPLOAD is confirm-required (it overwrites the FC mission), so `confirm:true` is
    required exactly as POST /api/commands demands. Returns { mission, command }."""
    now = datetime.now(timezone.utc)
    expire_commands(now)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"ok": False, "error": "bad_request",
                            "message": "Request body is not valid JSON."})

    vid = parse_vehicle_id(body.get("vehicle_id"))
    if vid not in known_vehicle_ids():
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "unknown vehicle", "vehicle_id": body.get("vehicle_id")})

    package = body.get("mission_package") if isinstance(body.get("mission_package"), dict) else None
    if not package or not isinstance(package.get("route_waypoints"), list):
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "invalid_mission_package",
            "message": "mission_package with route_waypoints is required."})

    if not bool(body.get("confirm")):
        return JSONResponse(status_code=409, content={
            "ok": False, "needs_confirmation": True, "type": "MISSION_UPLOAD",
            "message": "MISSION_UPLOAD overwrites the mission on the flight controller and "
                       "requires explicit confirmation. Resend with confirm:true."})

    # Canonicalize through the SAME function POST /api/commands uses — one contract, one hash.
    try:
        params = canonical_mission_upload_params({
            "contract_version": MISSION_CONTRACT_VERSION,
            "waypoints": package.get("route_waypoints")})
    except MissionContractError as exc:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "mission_contract_violation",
            "contract_version": MISSION_CONTRACT_VERSION,
            "message": "Mission does not satisfy " + MISSION_CONTRACT_VERSION + ".",
            "errors": exc.errors})

    # Defence-in-depth: the package's own route_hash (planning._route_hash) must match the
    # authoritative one derived here — otherwise the package was altered after generation.
    if package.get("route_hash") and package["route_hash"] != params["expected_route_content_hash"]:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "route_hash_mismatch",
            "message": "The mission package route_hash does not match its route waypoints — "
                       "regenerate the plan before finalizing."})

    # Optional, additive upload metadata forwarded VERBATIM to Scout in the command params
    # (agent_command_view). It carries no authority and does not affect the route/hash/counts
    # verification — a purely descriptive tag (e.g. "OPERATOR_REPLACEMENT"). Older Scouts ignore
    # an unknown params key, so this never breaks compatibility.
    upload_context = body.get("upload_context")
    if isinstance(upload_context, str) and upload_context.strip():
        params["upload_context"] = upload_context.strip()[:64]

    comm_state = comms_state_by_id.get(vid, "UNKNOWN")
    cmd = make_command(vid=vid, ctype="MISSION_UPLOAD", params=params,
                       created_by=body.get("created_by"), comm_state=comm_state, now=now,
                       source="OPERATOR")
    warnings = [RISK_WARNING["MISSION_UPLOAD"]]
    if comm_state == "PARTITIONED":
        warnings.append("Queued while communication is partitioned — delivery may be delayed.")
    elif comm_state == "DISCONNECTED":
        warnings.append("Queued while disconnected — will deliver on next contact.")
    cmd["warning"] = " ".join(warnings)

    rec = _new_mission_record(vid, package, cmd)
    _command_event(cmd, severity="caution",
                   message=f"Survey mission {rec['mission_id']} finalized and MISSION_UPLOAD "
                           f"created ({comm_state})",
                   source="operator-backend")
    return {"ok": True, "mission": rec, "command": cmd}


@app.get("/api/missions/original/{mission_id}")
def get_original_mission(mission_id: str):
    """The immutable original mission record (revision 0) for one mission id."""
    rec = original_missions.get(str(mission_id))
    if rec is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "not_found"})
    return {"ok": True, "mission": rec}


@app.get("/api/vehicles/{vehicle_id}/missions/active-original")
def get_active_original_mission(vehicle_id: str):
    """The most recently finalized original mission (revision 0) for a vehicle, or null."""
    vid = parse_vehicle_id(vehicle_id)
    mid = active_original_by_vehicle.get(vid)
    rec = original_missions.get(mid) if mid else None
    return {"ok": True, "vehicle_id": vid, "mission": rec}


@app.get("/api/commands/pending/{vehicle_id}")
def pending_commands(vehicle_id: str):
    """Commands awaiting the Local Agent for one vehicle. This fetch is the CLAIM: a
    QUEUED command transitions to SENT (claimed_at stamped) and is redelivered while
    SENT (at-least-once) until a result arrives — the Agent dedups by the command id."""
    now = datetime.now(timezone.utc)
    expire_commands(now)
    vid = parse_vehicle_id(vehicle_id)
    pending = []
    for cmd in commands:
        if cmd["vehicle_id"] != vid or cmd["status"] in TERMINAL_STATUSES:
            continue
        if cmd["status"] == "QUEUED":
            cmd["status"] = "SENT"
            cmd["claimed_at"] = now.isoformat()
            _refresh_command_derived(cmd)
            _command_event(cmd, severity="info",
                           message=f"Command {cmd['type']} sent to {cmd['vehicle']}",
                           source="operator-backend")
        pending.append(cmd)
    return {"vehicle_id": vid, "pending": pending, "generated_at": now.isoformat()}


def agent_command_view(cmd):
    """The Scout-facing view of a command record. The Local Agent's own field names
    (command_id / command_type) — deliberately NOT the internal record, which uses
    id/type and carries operator-side bookkeeping the vehicle has no business seeing
    (requested_comm_state, created_by, warning, …). `params` is always an object, never
    null, so the Agent can index it without a None check."""
    return {
        "command_id": cmd["id"],
        "command_type": cmd["type"],
        "source": cmd["source"],
        "params": cmd["params"] or {},
        "expires_at": cmd["expires_at"],
    }


@app.get("/agent/commands")
async def agent_commands(usv_id: str = ""):
    """Command delivery to the Scout Local Agent — the endpoint the DEPLOYED agent polls
    (`local_mission_agent/api_client.py`, configured `USV_ID = usv-2`):

        GET /agent/commands?usv_id=usv-2  →  {"commands": [ {command_id, command_type,
                                              params, expires_at}, ... ]}

    This is the Agent-facing twin of GET /api/commands/pending/{id} (the operator/debug
    view, which was the originally *planned* agent path — see docs/verification/
    commands.md). Both claim from the ONE queue with the SAME at-least-once semantics,
    differing only in field names; neither is a second source of truth.

    THE FIRST FETCH IS THE CLAIM: a QUEUED command moves QUEUED → SENT with claimed_at
    stamped, in the same pass that builds the response. `async def` with no awaits inside
    is what makes that atomic — the whole scan/mutate runs to completion on the event loop
    without yielding, so two concurrent polls can never claim the same command a plain
    `def` would run in the threadpool and could interleave).

    DELIVERY IS AT-LEAST-ONCE: a non-terminal command (QUEUED/SENT/ACCEPTED) is
    redelivered on every poll until a terminal result arrives or it expires. The Scout's
    Local Agent dedups by command_id — it records processed ids and rejects redeliveries
    without re-executing — so a repeat is harmless, while NOT redelivering would lose the
    command outright whenever a delivery response is dropped by an intermittent link
    (exactly the condition this system is built for). Same semantics as
    /api/commands/pending/{id}; the two endpoints differ only in field names.

    claimed_at records when the Agent FIRST took the command and is never rewritten by a
    redelivery — it is the claim time, not the last-seen time. The "sent to Scout" event
    likewise fires once, on the first claim, so a polling Agent cannot flood the log.

    Terminal and expired commands are never delivered (expire_commands runs first, so a
    command past its TTL is EXPIRED here rather than handed to the vehicle late).
    Never marks a command executed — only a Local Agent result can (POST
    /agent/command_result)."""
    now = datetime.now(timezone.utc)
    expire_commands(now)

    if not usv_id:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "missing usv_id",
            "expected": "GET /agent/commands?usv_id=usv-2"})

    vid = parse_vehicle_id(usv_id)
    if vid not in known_vehicle_ids():
        # Loud, not an empty list: a misconfigured USV_ID must never look like "no work
        # to do" forever — that is exactly the failure mode this endpoint was added for.
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "unknown vehicle", "usv_id": usv_id,
            # Canonical slugs: one stable, sortable spelling regardless of how each
            # vehicle's id is typed internally.
            "known": sorted(vehicle_slug(c) for c in known_vehicle_ids())})

    delivered = []
    for cmd in commands:
        if cmd["vehicle_id"] != vid or cmd["status"] in TERMINAL_STATUSES:
            continue
        if cmd["status"] == "QUEUED":
            # First delivery only: claim it. A redelivery re-sends the same record
            # untouched — claimed_at and the event both stay as they were.
            cmd["status"] = "SENT"
            cmd["claimed_at"] = now.isoformat()
            _refresh_command_derived(cmd)
            _command_event(cmd, severity="info",
                           message=f"Command {cmd['type']} sent to {cmd['vehicle']}",
                           source="operator-backend")
        delivered.append(agent_command_view(cmd))
    return {"commands": delivered, "usv_id": usv_id, "vehicle_id": vid,
            "generated_at": now.isoformat()}


@app.post("/api/commands/{command_id}/result")
async def command_result(command_id: str, request: Request):
    """Local Agent reports the outcome of a command (id in the PATH). Body:
    { status, result?, reason? } where status ∈ ACCEPTED|EXECUTED|REJECTED|FAILED.
    Idempotent — a result on an already-terminal command is a no-op (applied:false),
    so a re-sent ack never double-executes. This is the ONLY way a command becomes
    EXECUTED. Scout may instead POST /agent/command_result with the id in the body
    (same lifecycle, backlog-friendly) — both share process_command_result."""
    now = datetime.now(timezone.utc)
    # A non-JSON / malformed body must not 500 this endpoint — a Local Agent that acks
    # with the wrong content-type (or an empty body) still carries the command id in the
    # PATH, so we treat an unparseable body as an empty result and let the lifecycle logic
    # answer honestly (400 "missing status"), mirroring /agent/command_result's tolerance.
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        body = {}
    print(f"[COMMAND-RESULT] POST /api/commands/{command_id}/result body={body!r}")
    outcome = process_command_result(
        command_id, _pick(body, _RESULT_STATUS_KEYS),
        body.get("result"), _pick(body, _RESULT_REASON_KEYS), now,
        vehicle_id=_pick(body, _RESULT_VEHICLE_KEYS))
    print(f"[COMMAND-RESULT] command_id={command_id} found={outcome['found']} "
          f"applied={outcome['applied']} error={outcome.get('error')}")
    if not outcome["found"]:
        # The id lives in the PATH here: an unknown one is an unknown REST resource, so this
        # endpoint keeps the idiomatic 404 (Scout posts to /agent/command_result, not here, so
        # this 404 never drives a retry loop). The orphan was still archived for audit above.
        return JSONResponse(status_code=404, content={
            "ok": False, "error": outcome.get("error", "unknown command id"),
            "orphaned": outcome.get("orphaned", False), "command_id": command_id})
    if outcome.get("error"):
        return JSONResponse(status_code=400, content={
            "ok": False, "error": outcome["error"], "status": outcome.get("status"),
            "allowed": outcome.get("allowed")})
    return {"ok": True, "applied": outcome["applied"], "command": outcome["command"]}


@app.post("/agent/command_result")
async def agent_command_result(request: Request):
    """Command-result receiver expected by the Local Agent (Scout). The command id is in
    the BODY, not the path — this is the endpoint Scout was POSTing to (previously 405
    because only the id-in-path route existed). Accepts:
      • a legacy flat result:      { command_id, status, result?, reason?, vehicle_id? }
      • a canonical message envelope (2026-07-17 — what the deployed Scout's
        agent_buffer.jsonl actually contains): { message_type: "command_result",
        schema_version, source, target, timestamp, payload: { command_id, status,
        result?, reason?, ... } } — unwrapped to `payload` exactly once by
        _unwrap_envelope, then handled identically to the legacy flat form. The nested
        `result` (accepted/verified/ack_result/error/home_position/requested_position/
        verification_distance_m) is never flattened or altered — it is stored verbatim on
        the command record for _annotate_set_home_result to classify.
      • a flushed backlog: [ {...}, {...} ]  or  { results: [ {...}, ... ] } — each item
        may itself be EITHER a flat result or a full envelope; every item is unwrapped
        independently.
    Tolerant of field spellings (command_id/id/cmd_id, status/outcome, reason/error) and
    status aliases (TIMEOUT/ACK/DONE → the queue vocabulary). Idempotent by the uuid
    command id: replayed/duplicate results are no-ops (applied:false) — a flushed buffer
    never creates duplicate history rows or re-executes.

    A SINGLE result gets an honest HTTP status that distinguishes three outcomes:
      • MALFORMED (400) — no command id at all, or an invalid status on a KNOWN command:
        the request is broken and would stay broken on replay. A silent 200 here would tell
        the Agent its result "worked" while a known command stays non-terminal and keeps
        being redelivered by GET /agent/commands until the TTL, looking like the Agent never
        reported anything.
      • ORPHANED HISTORICAL RESULT (200, orphaned:true) — a well-formed result whose command
        id this process no longer holds (the in-memory queue was lost on a restart, or the id
        is bogus). Because the store is append-only within a process the id can never become
        known, so a 404 would only make the Agent retry an unresolvable result forever; we
        instead acknowledge it terminally and archive it as a low-severity audit event.
      • APPLIED / IDEMPOTENT NO-OP (200) — a known command, valid status: applied once,
        replays are no-ops (applied:false).
    A BATCH (2+ items, a flushed backlog) stays
    ALWAYS 2xx — per-item found/applied/orphaned/error carries the detail — so one unknown/
    already-terminal id in a backlog never fails the whole flush; see _result_items."""
    now = datetime.now(timezone.utc)
    try:
        body = await request.json()
    except Exception:
        body = None
    print(f"[COMMAND-RESULT] POST /agent/command_result body={body!r}")
    items = _result_items(body)
    if not items:
        print("[COMMAND-RESULT] rejected: no command results in body")
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "no command results in body",
            "expected": "{command_id, status} | [ ... ] | {results:[ ... ]}"})

    results = [
        process_command_result(
            _pick(it, _RESULT_ID_KEYS), _pick(it, _RESULT_STATUS_KEYS),
            it.get("result"), _pick(it, _RESULT_REASON_KEYS), now,
            vehicle_id=_pick(it, _RESULT_VEHICLE_KEYS))
        for it in items
    ]
    applied_n = sum(1 for r in results if r["applied"])
    for r in results:
        print(f"[COMMAND-RESULT] command_id={r['command_id']} found={r['found']} "
              f"applied={r['applied']} status={r.get('status')} error={r.get('error')}")

    # Strip the full command echo from batch items to keep the ack compact; a single
    # result keeps it for parity with the id-in-path endpoint.
    if len(results) == 1:
        r = results[0]
        # "missing command id" is a MALFORMED request (400) — the item never had an id to
        # look up. An unknown-but-present id is an ORPHANED HISTORICAL RESULT: the command is
        # gone (queue lost on restart / bogus id) and can never come back, so a 404 would only
        # make Scout retry it forever. We answer 200 — a TERMINAL ack that stops the retry —
        # having archived it as a low-severity audit event; found:false, orphaned:true and
        # applied:false in the body keep the answer honest for a body-inspecting Agent.
        if r.get("error") == "missing command id":
            status_code = 400
        elif r.get("orphaned"):
            status_code = 200
        elif not r["found"]:
            status_code = 404
        elif r.get("error"):
            status_code = 400
        else:
            status_code = 200
        return JSONResponse(status_code=status_code, content={
            "ok": status_code == 200, "applied": r["applied"], "found": r["found"],
            "orphaned": r.get("orphaned", False), "error": r.get("error"),
            "command": r.get("command"), "received": 1, "applied_count": applied_n})
    compact = [{k: v for k, v in r.items() if k != "command"} for r in results]
    return {"ok": True, "received": len(results), "applied_count": applied_n,
            "results": compact}


# --- Control authority (direct proxy to Scout Flask, NOT the command queue above) ---
# Take Control / Release Control in the Operator UI. Deliberately bypasses the
# QUEUED→SENT→EXECUTED command lifecycle entirely: control authority is vehicle
# state owned by Scout's own Flask service (motherpi/services/flask), reachable at
# VEHICLE_API_BASE. The operator backend holds no authority state of its own — every
# call here is a live, synchronous round-trip to Scout; a network failure surfaces
# as an honest reachable:false, never a guessed or cached value.
#
# Effective-authority semantics (the axis the operator acts on):
#   OPERATOR     — the operator holds the wheel; operator commands may execute.
#                  Requested via "Take Control".
#   LOCAL_AGENT  — the autonomous local agent holds the wheel; the mission runs.
#                  Requested via "Release Control".
#   RC           — an RC transmitter override has physically taken over. This is a
#                  *reported* effective state only: the operator can never request it
#                  (it is a hardware takeover), so it appears in reads, never in writes.
REQUESTABLE_AUTHORITY = ("OPERATOR", "LOCAL_AGENT")   # operator-initiated (POST)
REPORTABLE_AUTHORITY = ("OPERATOR", "LOCAL_AGENT", "RC")  # what a read may surface


def _scout_authority_read(vid: int, base: str):
    """Live GET of Scout's authority, normalized to a stable operator schema. Always
    returns a dict, never raises: an unreachable Scout is an honest reachable:false /
    authority:null so the frontend's 2 s poll never emits a console 4xx/5xx."""
    try:
        r = requests.get(f"{base}/agent/control_authority", timeout=3)
        r.raise_for_status()
        data = r.json() if r.content else {}
    except requests.RequestException as exc:
        return {
            "ok": True, "vehicle_id": vid, "available": True, "reachable": False,
            "authority": None, "source": "scout",
            "reason": "Scout control-authority API unreachable", "detail": str(exc),
        }
    raw = str(data.get("authority") or data.get("effective")
              or data.get("control_authority") or "").upper()
    value = raw if raw in REPORTABLE_AUTHORITY else None
    return {
        "ok": True, "vehicle_id": vid, "available": True, "reachable": True,
        "authority": value, "source": "scout", "raw": data,
    }


def cancel_pending_commands(vid: int, now, reason: str):
    """Terminate every non-terminal command for one vehicle. Reuses the existing
    EXPIRED terminal status — no new lifecycle/state machine — so a command left
    QUEUED/SENT while OPERATOR held authority can never fire once LOCAL_AGENT is
    re-engaged later. Queue-only; never touches Scout's own authority value."""
    for cmd in commands:
        if cmd["vehicle_id"] != vid or cmd["status"] in TERMINAL_STATUSES:
            continue
        cmd["status"] = "EXPIRED"
        cmd["completed_at"] = now.isoformat()
        cmd["reason"] = reason
        _refresh_command_derived(cmd)
        _command_event(cmd, severity="warning",
                       message=f"Command {cmd['type']} cancelled ({reason})",
                       source="operator-backend")


def apply_control_authority(vid: int, authority: str, *, source="operator"):
    """(payload, http_status) for ONE authority hand-off — the single implementation.

    Extracted from the POST route so the mission-lifecycle orchestration layer
    (mission_lifecycle.py) performs the SAME hand-off, with the same queue-safety
    cancellation and the same event, instead of a parallel copy that could drift. Scout
    remains the sole source of truth for the value itself; this returns Scout's
    acknowledged effective authority, never the requested one dressed up as confirmed.

    `source` only labels the event: an automatic transfer inside a Start/Resume/Stop
    transaction is recorded as such, so the event log distinguishes it from an operator
    pressing Take Control / Release Control by hand."""
    if authority not in REQUESTABLE_AUTHORITY:
        return {"ok": False, "error": "invalid authority", "authority": authority,
                "allowed": list(REQUESTABLE_AUTHORITY)}, 400
    if vid not in known_vehicle_ids():
        return {"ok": False, "error": "unknown vehicle", "vehicle_id": vid}, 404

    base = vehicle_api_base(vid)
    if base is None:
        return {
            "ok": False, "available": False, "vehicle_id": vid,
            "error": "no Scout control-authority API configured for this vehicle",
            "message": "This vehicle has no control-authority backend; authority cannot be "
                       "changed.",
        }, 409

    try:
        r = requests.post(f"{base}/agent/control_authority",
                          json={"authority": authority}, timeout=3)
        r.raise_for_status()
        result = r.json() if r.content else {}
    except requests.RequestException as exc:
        return {"ok": False, "error": "Scout control-authority API unreachable",
                "message": f"Scout control-authority API unreachable: {exc}",
                "detail": str(exc)}, 502

    if authority == "LOCAL_AGENT":
        cancel_pending_commands(vid, datetime.now(timezone.utc),
                                 "Cancelled — control released to Local Agent")

    eff = str(result.get("authority") or result.get("effective") or authority).upper()

    # First-class control-authority event (P3). Records the operator-initiated hand-off
    # with the Scout-confirmed effective value — a real transition worth a timestamp,
    # deduped so repeating the same request does not spam the log.
    eff_val = eff if eff in REPORTABLE_AUTHORITY else authority
    if last_authority_by_id.get(vid) != eff_val:
        last_authority_by_id[vid] = eff_val
        human = "Operator" if eff_val == "OPERATOR" else "Local Agent" if eff_val == "LOCAL_AGENT" else eff_val
        verb = "Take Control" if authority == "OPERATOR" else "Release Control"
        if source != "operator":
            verb = f"{source} transaction"
        _append_event(severity="caution" if eff_val == "OPERATOR" else "info",
                      # ASCII "->" — see the note in record_agent_changes: this string is
                      # printed to the console, where cp1252 cannot encode "→".
                      message=f"Control authority -> {human} ({verb})",
                      etype="authority", source="operator-backend",
                      vehicle_id=vid, vehicle=name_of(vid))
    return {
        "ok": True, "vehicle_id": vid, "requested": authority,
        "authority": eff if eff in REPORTABLE_AUTHORITY else None,
        "available": True, "reachable": True, "source": "scout", "raw": result,
    }, 200


def read_control_authority(vid: int):
    """The live authority read, in the same shape the GET route returns. Used by the route
    AND by the orchestration layer's read-back verification — a POST is never treated as a
    transfer until a READ confirms it."""
    if vid not in known_vehicle_ids():
        return {"ok": False, "error": "unknown vehicle", "vehicle_id": vid,
                "available": False, "reachable": False, "authority": None}
    base = vehicle_api_base(vid)
    if base is None:
        return {
            "ok": True, "vehicle_id": vid, "available": False, "reachable": False,
            "authority": None, "source": "scout",
            "reason": "No Scout control-authority API configured for this vehicle",
        }
    return _scout_authority_read(vid, base)


@app.post("/api/control_authority/{vehicle}")
async def set_control_authority(vehicle: str, request: Request):
    """Body: { "authority": "OPERATOR" | "LOCAL_AGENT" }. Take Control → OPERATOR,
    Release Control → LOCAL_AGENT (RC is a hardware takeover and is NOT requestable).
    Forwards to Scout's POST /agent/control_authority and returns a normalized ack
    ({requested, authority} where authority is Scout's acknowledged effective value)
    so the frontend confirms against the effective state, not the button press.

    This is the MANUAL override path and stays exactly that. Normal mission operation no
    longer depends on it: Start / Resume / Stop arrange and verify authority themselves
    (mission_lifecycle.py), so the operator never has to press Release Control to run a
    mission. Take Control remains available at all times.

    On a confirmed Release (authority=LOCAL_AGENT) also cancels any still-pending
    command-queue entries for this vehicle (queue safety — see cancel_pending_commands)
    so a stale operator command cannot fire once autonomy is back in control. Take
    Control does NOT cancel — the operator is taking the wheel. Scout remains the sole
    source of truth for the authority value itself."""
    body = await request.json()
    authority = str(body.get("authority") or "").upper()
    if authority not in REQUESTABLE_AUTHORITY:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "invalid authority",
            "authority": body.get("authority"), "allowed": list(REQUESTABLE_AUTHORITY)})
    payload, code = apply_control_authority(parse_vehicle_id(vehicle), authority)
    if code == 200:
        return payload
    return JSONResponse(status_code=code, content=payload)


@app.get("/api/control_authority/{vehicle}")
def get_control_authority(vehicle: str):
    """Live read of Scout's authority, normalized. Distinguishes three cases so the
    2 s poll is quiet and honest:
      unknown vehicle id          → deliberate JSON 404 (no such vehicle)
      known, no Scout API          → 200 available:false (nothing to read here)
      known, Scout API configured  → live proxy; reachable:false + authority:null if
                                      Scout does not answer (never a console 5xx)."""
    vid = parse_vehicle_id(vehicle)
    if vid not in known_vehicle_ids():
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "unknown vehicle", "vehicle_id": vehicle})
    return read_control_authority(vid)


# --- Scout's STABILIZED EVIDENCE (direct proxy to Scout Flask GET /agent/state) ---
# Scout stabilizes each raw MAVLink observation into an evidence record it stands behind:
#
#   { value, source, observed_at, age_s, state }   state ∈ FRESH | AGING | STALE | NEVER_OBSERVED
#
# and it is Scout's OWN age and Scout's OWN state — the operator station runs no TTL of its
# own and must not. A second, operator-side freshness rule would answer a different question
# (how old is this on MY clock, after MY poll interval) and would disagree with the very
# evidence Scout's risk model and feasibility gate are computed from. Displaying a
# disagreement as if it were the vehicle's state is exactly the fabrication this station
# forbids, so this route is a PASS-THROUGH: Scout's evidence dict, verbatim.
#
# WHY A SEPARATE READ AT ALL: the evidence block is NOT in the status packet Scout pushes to
# POST /agent/status (that payload carries telemetry/power/failsafe/imu/freshness/mavlink/
# health/mission/agent/... and no `evidence`). It exists only on the Flask API's
# GET /agent/state, port 8080 — the same VEHICLE_API_BASE map as control authority and the
# Pixhawk mission read-back, never the Local Agent's 8090, which 404s this path.
#
# `freshness` (Scout's older per-subsystem seconds map) is carried alongside because it is on
# the same body and the diagnostics page shows both; it is NOT merged into the evidence
# records and never substitutes for a missing one.
def read_agent_evidence(vid: int):
    """Scout's stabilized evidence + freshness for one vehicle, live. Always a dict, never
    raises: an unknown vehicle is a 404-shaped body, a vehicle with no Flask route is an
    honest available:false, and an unreachable Scout is reachable:false with `evidence:None`.
    NOTHING is defaulted — an absent evidence block stays absent, because a fabricated FRESH
    is the one answer that would be read as reassurance."""
    if vid not in known_vehicle_ids():
        return {"ok": False, "error": "unknown vehicle", "vehicle_id": vid,
                "available": False, "reachable": False, "evidence": None}
    base = vehicle_api_base(vid)
    if base is None:
        return {
            "ok": True, "vehicle_id": vehicle_slug(vid), "available": False, "reachable": False,
            "evidence": None, "freshness": None, "source": "scout",
            "reason": "No Scout API configured for this vehicle",
        }
    try:
        r = requests.get(f"{base}/agent/state", timeout=3)
        r.raise_for_status()
        data = r.json() if r.content else {}
    except requests.RequestException as exc:
        return {
            "ok": True, "vehicle_id": vehicle_slug(vid), "available": True, "reachable": False,
            "evidence": None, "freshness": None, "source": "scout",
            "reason": "Scout agent-state API unreachable", "detail": str(exc),
        }
    if not isinstance(data, dict):
        data = {}
    ev = data.get("evidence") if isinstance(data.get("evidence"), dict) else None
    fr = data.get("freshness") if isinstance(data.get("freshness"), dict) else None
    return {
        "ok": True, "vehicle_id": vehicle_slug(vid), "available": True, "reachable": True,
        "source": "scout",
        # Verbatim. A Scout that predates stabilized evidence reports none, and `evidence:None`
        # is what makes the UI read UNKNOWN rather than invent a state for every signal.
        "evidence": ev,
        "supported": ev is not None,
        "freshness": fr,
        "state_timestamp": data.get("state_timestamp"),
    }


@app.get("/api/vehicles/{vehicle_id}/agent/evidence")
def get_agent_evidence(vehicle_id: str):
    """Scout's own stabilized evidence records (value / source / observed_at / age_s / state)
    for one vehicle. Read-only pass-through of GET /agent/state — the operator station applies
    no TTL, computes no age and never upgrades a missing record to FRESH."""
    vid = parse_vehicle_id(vehicle_id)
    if vid not in known_vehicle_ids():
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "unknown vehicle", "vehicle_id": vehicle_id})
    return read_agent_evidence(vid)


# --- Pixhawk mission (direct proxy to Scout Flask, NOT the command queue) ---
# View-only readback of the mission currently STORED ON THE PIXHAWK for a vehicle
# (MISSION_REQUEST_LIST/MISSION_ITEM_INT over MAVLink, performed on Scout). Same
# proxy pattern as control_authority: the operator backend holds NO mission state of
# its own — every fetch is a live, synchronous round-trip to Scout's own Flask API
# (VEHICLE_API_BASE). A network failure surfaces as an honest reachable:false with an
# empty waypoint list, never a fabricated or cached mission. This is deliberately a
# separate axis from the operator command queue and from `mission_state` progress:
# it is what the flight controller actually holds, for testing/verification. The card
# is designed to later carry Compare / Upload / Export against this same payload.
#
# Scout-side contract (to be exposed by motherpi/services/flask, mirroring
# /agent/control_authority): GET /agent/pixhawk_mission returning any of the shapes
# normalized below. Until Scout ships it, this endpoint returns reachable:false and
# the operator UI reads "Scout unavailable" — never a guessed mission.

# MAVLink MAV_CMD codes → readable names for the waypoint popup. Unmapped codes fall
# back to "CMD <n>" so an unusual command is still shown honestly, never blanked.
MAV_CMD_NAMES = {
    16: "WAYPOINT", 17: "LOITER_UNLIM", 18: "LOITER_TURNS", 19: "LOITER_TIME",
    20: "RETURN_TO_LAUNCH", 21: "LAND", 22: "TAKEOFF", 82: "SPLINE_WAYPOINT",
    84: "VTOL_TAKEOFF", 85: "VTOL_LAND", 93: "DELAY", 177: "DO_JUMP",
    178: "DO_CHANGE_SPEED", 183: "DO_SET_SERVO", 189: "DO_LAND_START",
}
# Commands whose param1 encodes a loiter/hold time in seconds.
LOITER_TIME_CMDS = {19}


def _mission_coord(*vals):
    """First usable lat/lng from candidate fields. Accepts float degrees OR MAVLink
    int1e7 (MISSION_ITEM_INT x/y): anything with |value| > 180 is treated as degrees*1e7.
    Returns None when nothing usable is present (a waypoint with no position stays
    positionless rather than plotting at 0,0)."""
    for v in vals:
        if v is None or v == "":
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if abs(f) > 180:
            f = f / 1e7
        if f == 0:
            continue
        return f
    return None


def normalize_mission_item(item):
    """One Scout/MAVLink mission item → the stable waypoint schema the map overlay
    consumes. Tolerant of field-name spellings (seq/sequence, command/cmd, lat/x, …)
    so it works whether Scout forwards raw pymavlink items or a pre-shaped list."""
    if not isinstance(item, dict):
        return None
    seq = _first_present(item.get("seq"), item.get("sequence"), item.get("index"))
    cmd = _first_present(item.get("command"), item.get("cmd"), item.get("mav_cmd"))
    lat = _mission_coord(item.get("lat"), item.get("latitude"), item.get("x"))
    lng = _mission_coord(item.get("lng"), item.get("lon"), item.get("longitude"), item.get("y"))
    alt = _first_present(item.get("alt"), item.get("altitude"), item.get("z"))
    loiter = item.get("loiter_time")
    if loiter is None and cmd in LOITER_TIME_CMDS:
        loiter = item.get("param1")
    try:
        cmd_int = int(cmd) if cmd is not None else None
    except (TypeError, ValueError):
        cmd_int = None
    return {
        "seq": int(seq) if isinstance(seq, (int, float)) or (isinstance(seq, str) and seq.isdigit()) else seq,
        "command": cmd_int if cmd_int is not None else cmd,
        "command_name": item.get("command_name") or (MAV_CMD_NAMES.get(cmd_int, f"CMD {cmd_int}") if cmd_int is not None else None),
        "lat": lat,
        "lng": lng,
        "alt": round(float(alt), 2) if isinstance(alt, (int, float)) else None,
        "loiter_time": round(float(loiter), 1) if isinstance(loiter, (int, float)) else None,
        "frame": item.get("frame"),
    }


def _scout_mission_read(vid: int, base: str, now):
    """Live GET of the Pixhawk mission from Scout, normalized. Always returns a dict,
    never raises: an unreachable Scout is reachable:false with an empty list so the
    frontend's fetch never emits a console 4xx/5xx. `partial` marks an incomplete
    download (Scout said so, or fewer items arrived than the reported count)."""
    try:
        r = requests.get(f"{base}/agent/pixhawk_mission", timeout=8)
        r.raise_for_status()
        data = r.json() if r.content else {}
    except requests.RequestException as exc:
        return {
            "ok": True, "vehicle_id": vid, "available": True, "reachable": False,
            "fetched_at": now.isoformat(), "count": 0, "current_seq": None,
            "waypoints": [], "partial": False, "source": "scout",
            "reason": "Scout mission API unreachable", "detail": str(exc),
        }
    if not isinstance(data, dict):
        data = {}
    raw_items = (data.get("waypoints") or data.get("mission")
                 or data.get("items") or data.get("mission_items") or [])
    if not isinstance(raw_items, list):
        raw_items = []
    waypoints = [w for w in (normalize_mission_item(i) for i in raw_items) if w is not None]
    current = _first_present(data.get("current_seq"), data.get("current"),
                             data.get("current_wp"), data.get("current_waypoint"))
    try:
        current = int(current) if current is not None else None
    except (TypeError, ValueError):
        current = None
    # partial: Scout flagged it, OR the mission list is shorter than the count it
    # reported (a download that dropped items). Never claim complete when it isn't.
    reported = data.get("count")
    partial = bool(data.get("partial"))
    if isinstance(reported, int) and reported > len(waypoints):
        partial = True
    out = {
        "ok": True, "vehicle_id": vid, "available": True, "reachable": True,
        "fetched_at": now.isoformat(), "count": len(waypoints),
        "current_seq": current, "waypoints": waypoints, "partial": partial,
        "source": "scout", "raw_count": reported,
    }
    # mission-contract-v1 read-back fields. Passed through ONLY when Scout actually sends
    # them — never fabricated here. The operator backend stays a pure proxy for the
    # read-back: it does not compute a hash or a validity of its own, because the whole
    # value of the comparison is that the two sides derived their numbers independently.
    #
    # `route_waypoint_count` and `pixhawk_item_count` are the EXPLICIT counts and are what
    # verification and the UI use. Legacy `count`/`hash` continue to pass through for
    # compatibility, but `hash` is the FULL-mission hash (Home included) and must never be
    # compared against a route content hash — different bytes, different value.
    for k in ("contract_version", "pixhawk_item_count", "route_waypoint_count",
              "route_content_hash", "full_mission_hash", "hash", "loaded", "valid"):
        if k in data:
            out[k] = data[k]
    return out


@app.get("/api/vehicles/{vehicle_id}/pixhawk-mission")
def pixhawk_mission(vehicle_id: str):
    """Fetch the mission currently stored on the vehicle's Pixhawk (view-only). Live
    proxy to Scout — same three-case honesty as control authority:
      unknown vehicle id          → deliberate JSON 404 (no such vehicle)
      known, no Scout API          → 200 available:false (nothing to read here)
      known, Scout API configured  → live proxy; reachable:false + empty list if Scout
                                      does not answer (never a console 5xx)."""
    now = datetime.now(timezone.utc)
    vid = parse_vehicle_id(vehicle_id)
    if vid not in known_vehicle_ids():
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "unknown vehicle", "vehicle_id": vehicle_id})

    base = vehicle_api_base(vid)
    if base is None:
        return {
            "ok": True, "vehicle_id": vid, "available": False, "reachable": False,
            "fetched_at": now.isoformat(), "count": 0, "current_seq": None,
            "waypoints": [], "partial": False, "source": "scout",
            "reason": "No Scout mission API configured for this vehicle",
        }
    return _scout_mission_read(vid, base, now)


# --- Set Home (deployment: set the Pixhawk HOME_POSITION) ---
# SET_HOME is a normal queued command — create it via POST /api/commands like AUTO/RTL/
# LOITER/ARM/DISARM/PAUSE/RESUME (see COMMAND_TYPES/CONFIRM_REQUIRED_TYPES/RISK_WARNING
# above; SET_HOME already carries confirm-required + a risk warning there). There is no
# dedicated set-home route and the operator backend makes no direct HTTP call to Scout
# for it: QUEUED → SENT (Scout Local Agent polls GET /api/commands/pending/{id}) →
# EXECUTED/FAILED/REJECTED (Scout Local Agent reports via the normal command_result
# endpoints, same as every other command type). EXECUTED here means only "the Local
# Agent successfully called Scout Flask" — see _annotate_set_home_result for how the
# command's own nested result is classified for immediate feedback, and home_block()
# above for why the PERMANENT verified/not-verified state never comes from this at all.


@app.get("/api/command/{command_id}")
def command_by_id(command_id: str):
    """Look up ONE command by its uuid command id — the single-command read endpoint.

    Why this exists (see BACKEND_ROADMAP / commands read routes): the plural
    GET /api/commands/{vehicle_id} takes a VEHICLE id, so probing it with a command uuid
    quietly parses to vehicle_id -1 and returns an empty list — a confusing false negative
    when debugging a specific command. There is no GET /api/commands (only POST), so a
    bare GET there is 405. This SINGULAR /api/command/{command_id} route does not collide
    with either and resolves a command uuid directly against commands_by_id (the same dedup
    map the result endpoints use), returning the fully-derived record (verification +
    lifecycle refreshed) or a clean JSON 404. Read-only; never mutates the queue."""
    now = datetime.now(timezone.utc)
    expire_commands(now)
    cmd = commands_by_id.get(str(command_id))
    if cmd is None:
        # Distinguish a genuinely unknown command from an orphaned historical result the
        # backend archived after a restart — an operator debugging a uuid wants to know it
        # was seen, even though the live command is gone.
        orphan = orphaned_command_results.get(str(command_id))
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "unknown command id", "command_id": command_id,
            "orphaned_result": orphan})
    _refresh_command_derived(cmd)
    return {"ok": True, "command_id": command_id, "command": cmd}


@app.get("/api/commands/history/{vehicle_id}")
def command_history(vehicle_id: str):
    """Terminal (completed) commands for one vehicle, newest first — the command log."""
    now = datetime.now(timezone.utc)
    expire_commands(now)
    vid = parse_vehicle_id(vehicle_id)
    items = [c for c in commands
             if c["vehicle_id"] == vid and c["status"] in TERMINAL_STATUSES]
    items.reverse()
    return {"vehicle_id": vid, "commands": items, "generated_at": now.isoformat()}


@app.get("/api/commands/{vehicle_id}")
def vehicle_commands(vehicle_id: str):
    """Every command for one vehicle (active queue + history), newest first — the UI view."""
    now = datetime.now(timezone.utc)
    expire_commands(now)
    vid = parse_vehicle_id(vehicle_id)
    items = [c for c in commands if c["vehicle_id"] == vid]
    items.reverse()
    active = [c for c in items if c["status"] not in TERMINAL_STATUSES]
    return {"vehicle_id": vid, "commands": items, "active": active,
            "generated_at": now.isoformat()}


@app.post("/agent/status")
async def receive_agent_status(request: Request):
    incoming = await request.json()
    now = datetime.now(timezone.utc)
    vid = extract_usv_id(incoming)

    # An unidentified packet is REJECTED, not merged. Previously an unparseable identity
    # (e.g. a callsign like "SAR-001") fell back to id 2 — Scout — so a second vehicle's
    # telemetry, name and health silently overwrote Scout's record. Better a visible,
    # actionable rejection than cross-vehicle contamination.
    if vid is None:
        global _unidentified_log_at
        if (_unidentified_log_at is None
                or (now - _unidentified_log_at).total_seconds() >= STATUS_HEARTBEAT_SECONDS):
            _unidentified_log_at = now
            # ASCII only: this line goes to the Windows console, where a non-ASCII dash is
            # re-encoded into '?' noise by the default code page.
            print("[STATUS] accepted=false reason=unidentified_vehicle - packet carries no "
                  f"resolvable identity (expected one of {', '.join(_PACKET_ID_KEYS)} or "
                  "`source`); further identical rejections suppressed")
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "unidentified vehicle",
            "detail": "status packets must carry a vehicle identity in one of "
                      f"{list(_PACKET_ID_KEYS)} or the envelope `source`",
        })

    rec = vehicle_record(vid)

    # Monotonic current-state guard, PER VEHICLE (backend owns "now"): if this packet's own
    # send time is older than the newest we have already accepted FOR THIS VEHICLE, it is a
    # replayed/buffered snapshot and must not overwrite this vehicle's current state. It is
    # compared only against this vehicle's own newest timestamp — Scout's clock never gates
    # SAR's packets, and interleaved arrivals from any number of USVs never block each other.
    msg_ts = extract_message_ts(incoming)
    prev_ts = latest_msg_ts_by_id.get(vid)
    stale = msg_ts is not None and prev_ts is not None and msg_ts < prev_ts
    reject_reason = "stale_timestamp" if stale else None

    if not stale:
        # Everything below writes to exactly ONE vehicle's record, keyed by canonical id.
        rec["raw_latest"] = incoming
        rec["message_timestamp"] = msg_ts if msg_ts is not None else rec.get("message_timestamp")
        if msg_ts is not None:
            latest_msg_ts_by_id[vid] = msg_ts
        name = extract_name(incoming)
        if name:
            vehicle_names[vid] = str(name)
            rec["display_name"] = str(name)
        # Carry forward real telemetry values so a later degraded (telemetry-less)
        # packet renders last-known instead of a fabricated position.
        payload = incoming.get("payload", incoming) if isinstance(incoming, dict) else {}
        tel = payload.get("telemetry") if isinstance(payload, dict) else None
        if isinstance(tel, dict) and tel:
            lk = last_known_telemetry.setdefault(vid, {})
            # Carry forward only REAL readings. A battery of -1 (MAVLink "unknown") is a
            # transient absence, not a value — storing it would poison the last-known fallback
            # and let it flip a valid 97% to "—" on the next poll.
            lk.update({k: v for k, v in tel.items()
                       if v is not None and not (k == "battery" and v == -1)})
            rec["last_known_telemetry"] = lk
        # Same carry-forward for the agent reasoning group (payload.agent.*), and record
        # any agent-decision / mission-state CHANGE as a first-class event (deduped).
        agent_block = payload.get("agent") if isinstance(payload, dict) else None
        if isinstance(agent_block, dict) and agent_block:
            last_known_agent[vid] = dict(agent_block)
            rec["last_known_agent"] = last_known_agent[vid]
        # Remember every OTHER group this packet carried (power, failsafe, imu, freshness,
        # mavlink, communication, health, mission, service_status, measurements), so a later
        # partial update that omits one renders that vehicle's last snapshot instead of
        # wiping the row. Group-level only — a group that IS present replaces its stored
        # copy wholesale (see last_known_groups for why a deep merge would be wrong).
        if isinstance(payload, dict):
            vehicle_telemetry.observe_groups(last_known_groups, vid, payload)
        record_agent_changes(vid, payload, now)
        rec["packets"] = rec.get("packets", 0) + 1
        # The STREAK ends on recovery, but `last_reject` is deliberately NOT cleared: an
        # episode is usually investigated after it has already recovered, and evidence that
        # deletes itself on recovery is evidence you never get to read.
        recovered_after = rec.get("reject_streak", 0)
        rec["reject_streak"] = 0
    else:
        recovered_after = 0
        rec["rejected"] = rec.get("rejected", 0) + 1
        rec["reject_streak"] = rec.get("reject_streak", 0) + 1
        # Evidence for the rejection, kept on the record (and on GET /agent/status). `delta_s`
        # is the size of the backward step in the vehicle's OWN clock — the number that tells
        # a replayed store-and-forward packet (small negative) apart from a time base that
        # restarted (large negative, packet_ts near zero) or a poisoned high-water mark
        # (accepted_ts far ahead of wall clock).
        rec["last_reject"] = {
            "reason": reject_reason,
            "at": now.isoformat(),
            "accepted_ts": prev_ts,
            "packet_ts": msg_ts,
            "delta_s": round(msg_ts - prev_ts, 3),
            "streak": rec["reject_streak"],
        }

    # Arrival-age reachability is about *arrival*, not payload age: any packet that
    # reaches us (even a replayed one) proves the operator link is carrying data now,
    # so refresh THIS vehicle's last-seen and log CONNECTED for it alone. Buffered events
    # are still ingested as history (deduped) regardless of the snapshot guard.
    rec["received_at"] = now
    last_seen_by_id[vid] = now
    record_comms_state(vid, "CONNECTED", now, 0.0)
    ingest_payload_events(vid, incoming, now)

    # Packet-loss measurement is an ARRIVAL fact, so it is recorded here rather than in the
    # accepted branch: a packet whose payload lost the monotonic snapshot race still
    # physically arrived, and counting it as lost would overstate the link's badness.
    # Strictly this vehicle's own estimator — see packet_loss_by_id.
    arrival_payload = incoming.get("payload", incoming) if isinstance(incoming, dict) else {}
    if isinstance(arrival_payload, dict):
        arrival_comm = arrival_payload.get("communication")
        if isinstance(arrival_comm, dict):
            packet_loss_estimator(vid).observe(arrival_comm.get("seq"), now.timestamp())

    # One compact line per meaningful CHANGE — never the raw payload, and never once per
    # ~1 Hz packet (see status_log_decision / _status_signature above). The full envelope
    # is already available live in the Operator Station UI; dumping it here just buries the
    # terminal in noise that makes a real problem harder to spot. Real state changes still
    # get their own line elsewhere ([COMMS] transitions, [EVENT] agent/mission changes).
    # The line names the CANONICAL id and the reported source, so multi-USV routing — and a
    # vehicle whose packets start being rejected — is inspectable at a glance.
    log_payload = incoming.get("payload", incoming) if isinstance(incoming, dict) else {}
    if not isinstance(log_payload, dict):
        log_payload = {}
    log_source = (incoming.get("source") if isinstance(incoming, dict) else None) or name_of(vid)
    signature = _status_signature(log_payload, accepted=not stale, reason=reject_reason,
                                  source=log_source)
    should_log, why = status_log_decision(vid, signature, now)
    if should_log:
        line = (f"[STATUS] canonical_id={vehicle_slug(vid)} source={log_source} "
                f"accepted={str(not stale).lower()}")
        if stale:
            # Enough evidence to diagnose WHY without a second run: the vehicle's own clock
            # values, not just the verdict. A backward step (negative delta) distinguishes a
            # replayed/buffered packet from a Local Agent whose time base restarted.
            line += (f" reason={reject_reason} prev_ts={prev_ts!r} msg_ts={msg_ts!r}"
                     f" delta_s={round(msg_ts - prev_ts, 3)}")
        elif recovered_after:
            # Closes the episode in the log: how many packets that vehicle lost, so a
            # rejection streak is visible end-to-end without reading every line.
            line += f" recovered_after={recovered_after}"
        log_mission = log_payload.get("mission") if isinstance(log_payload.get("mission"), dict) else {}
        # BOTH of these are the VEHICLE'S OWN WORDS, taken verbatim out of the packet, and the
        # names say so. Unqualified `comm=` / `mission=` read as the operator's verdict, and that
        # produced two contradictions that looked like bugs and were not:
        #   comm=PARTITIONED while the UI showed CONNECTED — the operator's comm state is
        #     ARRIVAL-AGE derived (build_vehicle_view), so a packet that reaches us proves the
        #     link is up now even when the payload self-reports a partition it saw earlier.
        #     `link=` below is that operator-side verdict, so the two are visible side by side.
        #   mission=IDLE while the Agent Mission card showed RUNNING — this is the SUPERVISORY
        #     agent's decision state (payload.mission.mission_state), not Scout's mission-
        #     execution lifecycle (GET /agent/mission_execution/status, port 8090), which this
        #     log line has never carried and must not be read as.
        line += (f" agent_comm={log_payload.get('comm_state', 'UNKNOWN')}"
                 f" link={comms_state_by_id.get(vid, 'UNKNOWN')}"
                 f" agent_mission={log_mission.get('mission_state', '—')} ({why})")
        print(line)

    return {
        "ok": True,
        "message": "status received",
        "stale": stale,
        "reason": reject_reason,
        "vehicle_id": vehicle_slug(vid),
        "received_at": now.isoformat(),
    }


@app.get("/agent/status")
def get_agent_status():
    """The last accepted packet per vehicle. `latest_status`/`received_at` keep the old
    single-vehicle shape for existing callers (most recently contacted vehicle), while
    `vehicles` exposes the real per-USV truth — there is no global "latest" state any more."""
    per_vehicle = {}
    newest = None
    for cid, rec in current_vehicle_state.items():
        per_vehicle[vehicle_slug(cid)] = {
            "vehicle_id": vehicle_slug(cid),
            "id": cid,
            "name": name_of(cid),
            "latest_status": rec.get("raw_latest"),
            "received_at": rec["received_at"].isoformat() if rec.get("received_at") else None,
            "message_timestamp": rec.get("message_timestamp"),
            # Per-USV ingest counters + the most recent rejection's evidence. This is how a
            # "vehicle X was accepted=false for a while" episode is diagnosed after the fact:
            # `last_reject.delta_s` is how far that vehicle's own clock went backwards.
            "accepted_packets": rec.get("packets", 0),
            "rejected_packets": rec.get("rejected", 0),
            "reject_streak": rec.get("reject_streak", 0),   # 0 = currently accepting
            "last_reject": rec.get("last_reject"),          # survives recovery, for post-hoc use
        }
        if rec.get("received_at") and (newest is None or rec["received_at"] > newest[1]):
            newest = (cid, rec["received_at"])
    latest = current_vehicle_state.get(newest[0]) if newest else None
    return {
        "latest_status": latest.get("raw_latest") if latest else {},
        "received_at": newest[1].isoformat() if newest else None,
        "vehicles": per_vehicle,
    }


# --- Bounded status diagnostics ----------------------------------------------------
# Multi-USV routing has to be inspectable, but two vehicles posting at ~1 Hz means ~120
# lines a minute if every packet logs — the real transitions ([COMMS], [EVENT], a packet
# starting to be rejected) drown in it. So [STATUS] is CHANGE-DRIVEN and per-vehicle:
# a line is printed on first contact, whenever this vehicle's status signature changes
# (accepted↔rejected, a new rejection reason, comm-state, mission state, mode, armed, or
# the source string that resolved to this canonical id), and otherwise at most once per
# STATUS_HEARTBEAT_SECONDS as a liveness confirmation. Steady-state traffic is silent;
# a repeated identical rejection prints once, not once per packet.
#
# Dedup state is strictly per canonical id, so a chatty vehicle can never suppress or
# trigger another vehicle's line.
STATUS_HEARTBEAT_SECONDS = 60

_status_log_state = {}   # {canonical_id: {"signature": tuple, "at": datetime}}


def status_log_decision(cid, signature, now):
    """(should_log, why) for one vehicle's status line. Pure apart from the per-vehicle
    dedup state it updates on a decision to log. `why` is "first-contact" | "change" |
    "heartbeat" and is only meaningful when should_log is True."""
    prev = _status_log_state.get(cid)
    if prev is None:
        _status_log_state[cid] = {"signature": signature, "at": now}
        return True, "first-contact"
    if signature != prev["signature"]:
        prev["signature"] = signature
        prev["at"] = now
        return True, "change"
    if (now - prev["at"]).total_seconds() >= STATUS_HEARTBEAT_SECONDS:
        prev["at"] = now
        return True, "heartbeat"
    return False, None


def _status_signature(payload, *, accepted, reason, source):
    """The facts a [STATUS] line reports. Two packets with the same signature say nothing
    new, so only the first of them is printed. Deliberately excludes continuously varying
    telemetry (battery, position, heading) — those are the UI's job, not the log's."""
    tel = payload.get("telemetry") if isinstance(payload.get("telemetry"), dict) else {}
    mission = payload.get("mission") if isinstance(payload.get("mission"), dict) else {}
    return (
        accepted,
        reason,
        payload.get("comm_state", "UNKNOWN"),
        mission.get("mission_state"),
        tel.get("mode"),
        tel.get("armed"),
        source,          # a changed source string means identity resolution changed
    )


# Unidentified packets carry no vehicle to key dedup state by, so they get their own
# rate limit — a misconfigured agent retrying at 1 Hz must not flood the terminal.
_unidentified_log_at = None


# Bounded fleet diagnostics: one line only when the fleet's shape actually changes
# (vehicle count or per-state counts), never once per 2 s poll. Enough to see "both USVs
# are present and connected" without flooding the terminal.
_last_fleet_summary = None


def _log_fleet_summary(fleet):
    global _last_fleet_summary
    counts = {"CONNECTED": 0, "PARTITIONED": 0, "DISCONNECTED": 0, "UNKNOWN": 0}
    for v in fleet:
        counts[v.get("comm_state", "UNKNOWN")] = counts.get(v.get("comm_state", "UNKNOWN"), 0) + 1
    summary = (len(fleet), tuple(sorted(counts.items())))
    if summary == _last_fleet_summary:
        return
    _last_fleet_summary = summary
    print(f"[FLEET] vehicles={len(fleet)} connected={counts['CONNECTED']} "
          f"partitioned={counts['PARTITIONED']} disconnected={counts['DISCONNECTED']} "
          f"unknown={counts['UNKNOWN']}")


@app.get("/api/fleet/status")
def fleet_status():
    """Every known vehicle, every time — each row normalized INDEPENDENTLY from that
    vehicle's own record and its own last-contact time.

    This endpoint used to build a static template list and splice in the single most
    recently received packet, so exactly one vehicle could be populated at a time and the
    others reverted to UNKNOWN placeholders whenever someone else posted. Now nothing about
    vehicle A's row is a function of vehicle B: a vehicle that did not report during this
    poll keeps its last-known values and ages on its own clock (CONNECTED → PARTITIONED →
    DISCONNECTED), and no vehicle ever disappears because another one reported."""
    fleet = []
    for cid in fleet_vehicle_ids():
        rec = current_vehicle_state.get(cid)
        if rec is None or rec.get("raw_latest") is None:
            # Configured but never contacted (or contacted with nothing storable yet).
            row = never_contacted_row(cid)
            if rec is not None and rec.get("received_at") is not None:
                row["last_seen"] = rec["received_at"].isoformat()
            fleet.append(row)
            continue
        fleet.append(normalize_agent_message(
            rec["raw_latest"], cid=cid, received_at=rec.get("received_at")))
    _log_fleet_summary(fleet)
    return fleet


def summarize_comms_durations(transitions, now):
    """Total seconds spent in each comm-state (last segment runs to `now`)."""
    durations = {}
    for i, tr in enumerate(transitions):
        start = datetime.fromisoformat(tr["ts"])
        end = datetime.fromisoformat(transitions[i + 1]["ts"]) if i + 1 < len(transitions) else now
        durations[tr["state"]] = round(durations.get(tr["state"], 0.0) + (end - start).total_seconds(), 1)
    return durations


@app.get("/api/comms/history/{vehicle_id}")
def comms_history(vehicle_id: str):
    """Operator-side comms-state transition log for one vehicle.

    Accepts the id in either form the rest of the system uses — '2' or 'usv-2' (the
    Scout's source id) — so callers don't have to guess. Powers the Map comms
    timeline, the Autonomy decision-trace comms nodes, and the thesis 'total
    disconnected time' metric. Empty transitions => never contacted.
    """
    now = datetime.now(timezone.utc)
    vid = parse_vehicle_id(vehicle_id)
    transitions = comms_history_by_id.get(vid, [])
    return {
        "vehicle_id": vid,
        "current": comms_state_by_id.get(vid, "UNKNOWN"),
        "transitions": transitions,
        "durations_s": summarize_comms_durations(transitions, now),
        "generated_at": now.isoformat(),
    }


@app.get("/api/events")
def get_events(limit: int = 500):
    """Persistent operator event log (comms transitions + vehicle-reported events).

    Single backend source for the Events page (and later Timeline / Replay / stats).
    Returns events in chronological order (oldest→newest); the frontend sorts for
    display. `limit` caps to the most recent N. Empty => nothing has happened yet.
    """
    now = datetime.now(timezone.utc)
    items = event_log[-limit:] if limit and limit > 0 else list(event_log)
    return {
        "events": items,
        "count": len(event_log),
        "generated_at": now.isoformat(),
    }


# Last successful weather read (source freshness / graceful degradation). A transient
# open-meteo failure then shows the last-known values dimmed with an age, rather than
# blanking the widget or — worse — 500ing the whole endpoint.
_env_cache = {"data": None, "fetched_at": None}
_ENV_KEYS = ("temperature", "weather_code", "wind_speed", "wind_direction")


def safe_local_time():
    """Local wall-clock string that never raises. ZoneInfo needs the tz database, which
    is absent on some hosts (no `tzdata`, no system zoneinfo) — there it raises
    ZoneInfoNotFoundError. Falling back to a fixed Europe/Stockholm offset (CET/CEST is
    a display nicety, not safety data) keeps the endpoint from 500ing on those hosts."""
    try:
        return datetime.now(ZoneInfo("Europe/Stockholm")).strftime("%H:%M:%S")
    except Exception:
        # Approximate CET/CEST without tz data: UTC+1, +2 during summer DST months.
        now = datetime.now(timezone.utc)
        offset = 2 if 3 <= now.month <= 10 else 1
        return (now + timedelta(hours=offset)).strftime("%H:%M:%S")


@app.get("/api/environment")
def environment():
    """Weather/wind for the Map overlay. ALWAYS returns the same stable schema and
    NEVER 500s — missing or unreachable sensors become null values with an
    `available`/freshness indication, so the frontend can hide or dim the widget
    instead of erroring. `available` = this response carries a real reading;
    `stale` + `source_age_s` mark a served-from-cache reading after a fetch failure."""
    now = datetime.now(timezone.utc)
    lat, lng = 56.699893, 13.002148
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lng}"
        "&current=temperature_2m,weather_code,wind_speed_10m,wind_direction_10m"
        "&timezone=Europe%2FStockholm"
    )

    base = {"local_time": safe_local_time(), "generated_at": now.isoformat()}

    try:
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        current = (r.json() or {}).get("current", {}) or {}
        data = {
            "temperature": current.get("temperature_2m"),
            "weather_code": current.get("weather_code"),
            "wind_speed": current.get("wind_speed_10m"),
            "wind_direction": current.get("wind_direction_10m"),
        }
        _env_cache["data"] = data
        _env_cache["fetched_at"] = now
        return {**base, **data, "available": True, "stale": False,
                "source": "open-meteo", "source_age_s": 0}
    except Exception as e:
        # Never 500: serve last-known (dimmed, with age) if we have it, else all-null.
        cached = _env_cache["data"]
        if cached:
            age = (now - _env_cache["fetched_at"]).total_seconds() if _env_cache["fetched_at"] else None
            return {**base, **cached, "available": False, "stale": True,
                    "source": "open-meteo", "source_age_s": round(age, 1) if age is not None else None,
                    "error": str(e)}
        return {**base, **{k: None for k in _ENV_KEYS}, "available": False,
                "stale": False, "source": "open-meteo", "source_age_s": None,
                "error": str(e)}


# --- Network-impairment experiment (Stage 1) — Operator→Scout orchestration proxy ---
# A thin proxy to Scout's experiment controller (GET/POST/DELETE {base}/agent/experiment/
# network), resolved through the SAME VEHICLE_API_BASE map as control_authority / pixhawk_
# mission — never a hard-coded address, and never one the browser owns. The Operator backend:
#   • validates the frontend profile against STAGE-1 CAPABILITIES before forwarding, so an
#     unsupported field fails HERE with a clear 400 instead of a Scout 500;
#   • GENERATES the experiment_id (a UUID per accepted apply) — the browser never sends one;
#   • forwards a normalized request to Scout and returns ONLY Scout-confirmed state (never
#     optimistically active — GET polling is the source of truth);
#   • records durable-within-process history (same in-memory append-only pattern as
#     event_log / commands) and mirrors each action into the operator event log.
#
# Scope: thesis experiment infrastructure on the Operator↔Scout communications link — NOT a
# Pixhawk vehicle command. It is deliberately INDEPENDENT of control authority and of the
# comm-state command gate (no OPERATOR/LOCAL_AGENT check anywhere in this block).
EXPERIMENT_STAGE = 1
# Stage 1 is scout_to_operator netem only. Everything else is a known Scout gap.
EXPERIMENT_SUPPORTED_DIRECTIONS = {"scout_to_operator"}

# Range limits — MIRROR the frontend LIMITS (operator/lib/experiment.js) so a value the UI
# accepts is never rejected here for a different reason (and vice-versa).
EXPERIMENT_LIMITS = {
    "latency_ms": (0, 10000),
    "jitter_ms": (0, 5000),
    "packet_loss_pct": (0, 100),
    "duration_s": (1, 3600),
}

# Timeout policy. connect is fixed and short; read is LATENCY-AWARE because a scout_to_operator
# impairment delays Scout's RESPONSE — a fixed short read timeout would misclassify a
# legitimately-delayed latency experiment as a failure. Firmly upper-bounded (READ_CAP) so a
# pathological latency value can never hang the endpoint. GET/DELETE size their read timeout
# from the KNOWN active profile (a big latency experiment delays their responses too).
EXPERIMENT_CONNECT_TIMEOUT = 3.0
EXPERIMENT_READ_BASE = 5.0
EXPERIMENT_READ_CAP = 20.0
EXPERIMENT_READ_SAFETY = 2.0

MAX_EXPERIMENT_HISTORY = 1000
# Append-only, in-memory (resets on restart), exactly like event_log / commands / comms
# history — the established persistence pattern in this process, not a new database.
experiment_history = []          # [ {timestamp, experiment_id, vehicle_id, action, direction,
                                 #    profile, duration_s, result, detail} ]
# The no-id GET/DELETE the frontend issues resolves to the last vehicle a POST/DELETE
# targeted; before any POST it defaults to the first configured route (Scout). This is the
# ONE place an absent id picks a vehicle, and it applies only when the caller named none —
# an id that IS supplied but names no vehicle is rejected, never silently redirected here.
_last_experiment_vehicle_id = next(iter(VEHICLE_API_BASE), 2)
# Per-vehicle tracking so GET can (a) size its own latency-aware read timeout from the known
# active profile and (b) record confirmed_active / expired_automatically exactly once per
# experiment_id as Scout's polled state transitions.
_experiment_tracked = {}         # {vid: {experiment_id, active, direction, profile, recorded:set}}


def _experiment_vehicle_id(raw):
    """Canonical target vehicle for an experiment call, in any accepted spelling.

    Returns the canonical id, or the sentinel -1 when the caller SUPPLIED an id that names
    no vehicle this station knows. Only a genuinely absent id (None / "") falls back to the
    last-targeted vehicle: with more than one routable USV configured, quietly resolving an
    unrecognised id to a default would point an impairment command at the wrong vehicle."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return _last_experiment_vehicle_id
    return parse_vehicle_id(raw)


def _experiment_read_timeout(latency_ms=0, jitter_ms=0):
    """Read timeout that ACCOUNTS for the requested (or active) Scout→Operator delay, with a
    firm upper bound. A 500 ms latency experiment must not be called a failure just because
    the ack took ~500 ms; a pathological 10 s value must still never hang past READ_CAP."""
    try:
        delay_s = (float(latency_ms or 0) + float(jitter_ms or 0)) / 1000.0
    except (TypeError, ValueError):
        delay_s = 0.0
    return max(EXPERIMENT_READ_BASE,
               min(EXPERIMENT_READ_CAP, EXPERIMENT_READ_BASE + EXPERIMENT_READ_SAFETY * delay_s))


def _experiment_tracker(vid):
    return _experiment_tracked.setdefault(
        vid, {"experiment_id": None, "active": False, "direction": None,
              "profile": {}, "recorded": set()})


def _record_experiment_history(*, vehicle_id, action, result, experiment_id=None,
                               direction=None, profile=None, duration_s=None, detail=None,
                               severity="info", event=True):
    """Append one experiment-history record and (by default) mirror it into the operator
    event log so the Events page shows it. `action` ∈ requested | confirmed_active | rejected
    | apply_failed | stopped_manually | expired_automatically."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "experiment_id": experiment_id,
        "vehicle_id": vehicle_id,
        "action": action,
        "direction": direction,
        "profile": profile or {},
        "duration_s": duration_s,
        "result": result,
        "detail": detail,
    }
    experiment_history.append(entry)
    if len(experiment_history) > MAX_EXPERIMENT_HISTORY:
        del experiment_history[0:len(experiment_history) - MAX_EXPERIMENT_HISTORY]
    if event:
        _append_event(
            severity=severity,
            message=f"Experiment {action.replace('_', ' ')} ({result})",
            etype="experiment", source="operator-backend",
            vehicle_id=vehicle_id, vehicle=name_of(vehicle_id),
            detail={"experiment_id": experiment_id, "action": action, "result": result,
                    "direction": direction, "profile": profile or {}, "duration_s": duration_s},
        )
    return entry


def _experiment_num(v):
    """Coerce a numeric field; None for blank, the "INVALID" sentinel for non-numeric."""
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return "INVALID"


def _experiment_positive(v):
    n = _experiment_num(v)
    return isinstance(n, float) and n > 0


def experiment_unsupported(body):
    """Stage-1-unsupported aspects of a requested profile (empty list => all Stage 1).
    Rejected loudly rather than forwarded, so an unsupported field never becomes a Scout 500."""
    unsupported = []
    direction = str(body.get("direction") or "").strip()
    if direction and direction not in EXPERIMENT_SUPPORTED_DIRECTIONS:
        unsupported.append(f"direction={direction}")
    if body.get("bandwidth_kbit_s") not in (None, ""):
        unsupported.append("bandwidth_kbit_s")
    if _experiment_positive(body.get("duplication_pct")):
        unsupported.append("duplication_pct")
    if _experiment_positive(body.get("reordering_pct")):
        unsupported.append("reordering_pct")
    if body.get("full_disconnect") is True:
        unsupported.append("full_disconnect")
    return unsupported


def experiment_invalid_ranges(body):
    """Out-of-range / non-numeric SUPPORTED fields (empty dict => all in range)."""
    invalid = {}
    for field, (lo, hi) in EXPERIMENT_LIMITS.items():
        n = _experiment_num(body.get(field))
        if n is None or n == "INVALID":
            invalid[field] = "must be a number"
        elif n < lo or n > hi:
            invalid[field] = f"must be between {lo} and {hi}"
    return invalid


def _stable_experiment_state(vid, *, status, active, experiment_id=None, started_at=None,
                             ends_at=None, remaining_s=None, direction=None, profile=None,
                             error=None, available=True):
    """The ONE stable schema every experiment endpoint returns — so the frontend never has
    to guess field presence. `active` is the ONLY thing that may drive the ACTIVE badge."""
    return {
        "status": status, "active": bool(active), "experiment_id": experiment_id,
        "vehicle_id": vid, "started_at": started_at, "ends_at": ends_at,
        "remaining_s": remaining_s, "direction": direction, "profile": profile,
        "error": error, "available": available,
    }


def _normalize_scout_experiment(vid, data):
    """Scout's confirmed experiment state → the stable operator schema. Tolerant of field
    spellings and of a flat profile; `active` follows Scout's own flag, never assumed."""
    if not isinstance(data, dict):
        data = {}
    active = bool(data.get("active"))
    exp_id = data.get("experiment_id") or data.get("id")
    profile = data.get("profile") if isinstance(data.get("profile"), dict) else None
    if profile is None and active:
        flat = {k: data.get(k) for k in ("latency_ms", "jitter_ms", "packet_loss_pct",
                "bandwidth_kbit_s", "duplication_pct", "reordering_pct", "full_disconnect")
                if data.get(k) is not None}
        profile = flat or None
    return _stable_experiment_state(
        vid,
        status="active" if active else "inactive",
        active=active,
        experiment_id=exp_id,
        started_at=_first_present(data.get("started_at"), data.get("start")),
        ends_at=_first_present(data.get("ends_at"), data.get("end")),
        remaining_s=_first_present(data.get("remaining_s"), data.get("remaining")),
        direction=data.get("direction"),
        profile=profile,
        error=data.get("error"),
        available=True,
    )


def _observe_experiment_state(vid, state):
    """React to a Scout-confirmed state (from POST or a GET poll). Records confirmed_active /
    expired_automatically exactly ONCE per experiment_id as the state transitions, and keeps
    the tracked profile fresh so GET's read timeout stays latency-aware. GET is the source of
    truth for these lifecycle events — never optimistic."""
    global _last_experiment_vehicle_id
    t = _experiment_tracker(vid)
    exp_id = state.get("experiment_id")
    if state.get("active") is True:
        _last_experiment_vehicle_id = vid
        t["experiment_id"] = exp_id
        t["direction"] = state.get("direction")
        if isinstance(state.get("profile"), dict):
            t["profile"] = state["profile"]
        key = ("confirmed_active", exp_id)
        if exp_id and key not in t["recorded"]:
            t["recorded"].add(key)
            _record_experiment_history(
                vehicle_id=vid, action="confirmed_active", result="active",
                experiment_id=exp_id, direction=state.get("direction"),
                profile=state.get("profile"))
        t["active"] = True
    else:
        prev_id = t.get("experiment_id")
        if t.get("active") and prev_id and ("terminal", prev_id) not in t["recorded"]:
            t["recorded"].add(("terminal", prev_id))
            _record_experiment_history(
                vehicle_id=vid, action="expired_automatically", result="expired",
                experiment_id=prev_id, direction=t.get("direction"),
                profile=t.get("profile"))
        t["active"] = False
        t["profile"] = {}


@app.get("/api/experiment/network")
def get_network_experiment(vehicle_id: Optional[str] = None):
    """Confirmed Scout experiment state for the selected/last-targeted vehicle, in the stable
    schema. Never 500s: an unreachable Scout is a deliberate, handled unavailable response so
    the frontend's 2 s poll renders an honest "Unavailable" (its convention for a failed
    experiment GET) rather than a fabricated inactive/active state."""
    vid = _experiment_vehicle_id(vehicle_id)
    base = vehicle_api_base(vid)
    if base is None:
        return JSONResponse(status_code=200, content=_stable_experiment_state(
            vid, status="unavailable", active=False, available=False,
            error="No Scout experiment controller configured for this vehicle"))
    prof = (_experiment_tracked.get(vid) or {}).get("profile") or {}
    read = _experiment_read_timeout(prof.get("latency_ms"), prof.get("jitter_ms"))
    try:
        r = requests.get(f"{base}/agent/experiment/network",
                         timeout=(EXPERIMENT_CONNECT_TIMEOUT, read))
        r.raise_for_status()
        data = r.json() if r.content else {}
    except requests.RequestException:
        # Non-500, stable, documented: 503 makes the existing frontend show "Unavailable"
        # (a failed GET is its honest unavailable signal) without any frontend change.
        return JSONResponse(status_code=503, content=_stable_experiment_state(
            vid, status="unavailable", active=False, available=False,
            error="Scout experiment controller unreachable"))
    state = _normalize_scout_experiment(vid, data)
    _observe_experiment_state(vid, state)
    return state


@app.post("/api/experiment/network")
async def apply_network_experiment(request: Request):
    """Apply an impairment profile: validate (capabilities + ranges) → generate the
    experiment UUID → forward the normalized request to Scout → return Scout-confirmed state.
    Never optimistically active. Because scout_to_operator impairment can delay or drop the
    apply ack, a forward failure is recorded but NOT declared a failed experiment — GET polling
    remains the source of truth."""
    global _last_experiment_vehicle_id
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    # Any accepted spelling (3, "3", "usv-3", "SAR-001") resolves to the same canonical
    # vehicle. A supplied id that names no vehicle is the -1 sentinel and falls through to
    # the no-route 409 below — it must never quietly become the last-targeted USV, which
    # would apply an impairment profile to a vehicle the operator did not choose.
    raw_vid = body.get("vehicle_id")
    if isinstance(raw_vid, bool):
        raw_vid = None
    vid = _experiment_vehicle_id(raw_vid)

    base = vehicle_api_base(vid)
    if base is None:
        return JSONResponse(status_code=409, content=_stable_experiment_state(
            vid, status="unavailable", active=False, available=False,
            error="No Scout experiment controller configured for this vehicle"))

    # Capability gate (Stage 1) — reject clearly, never forward to a Scout 500.
    unsupported = experiment_unsupported(body)
    if unsupported:
        _record_experiment_history(
            vehicle_id=vid, action="rejected", result="unsupported",
            direction=body.get("direction"), detail={"unsupported": unsupported},
            severity="caution")
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "unsupported experiment profile",
            "unsupported": unsupported, "supported_stage": EXPERIMENT_STAGE})

    # Range gate — reject out-of-range values BEFORE forwarding.
    invalid = experiment_invalid_ranges(body)
    if invalid:
        _record_experiment_history(
            vehicle_id=vid, action="rejected", result="invalid_range",
            direction=body.get("direction"), detail={"invalid": invalid},
            severity="caution")
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "invalid experiment parameters", "invalid": invalid})

    experiment_id = str(uuid.uuid4())     # backend-owned; the browser never supplies one
    _last_experiment_vehicle_id = vid

    # Normalized forward payload (Stage 1). The unsupported fields have all been validated to
    # their harmless defaults above, so they are forwarded verbatim — matching the Scout
    # contract shape exactly, with experiment_id injected and vehicle_id dropped.
    scout_payload = {
        "experiment_id": experiment_id,
        "latency_ms": body.get("latency_ms"),
        "jitter_ms": body.get("jitter_ms"),
        "packet_loss_pct": body.get("packet_loss_pct"),
        "bandwidth_kbit_s": body.get("bandwidth_kbit_s"),
        "duplication_pct": body.get("duplication_pct", 0),
        "reordering_pct": body.get("reordering_pct", 0),
        "full_disconnect": bool(body.get("full_disconnect", False)),
        "direction": "scout_to_operator",
        "duration_s": body.get("duration_s"),
    }
    profile = {k: scout_payload[k] for k in (
        "latency_ms", "jitter_ms", "packet_loss_pct", "bandwidth_kbit_s",
        "duplication_pct", "reordering_pct", "full_disconnect")}
    _record_experiment_history(
        vehicle_id=vid, action="requested", result="forwarded",
        experiment_id=experiment_id, direction="scout_to_operator",
        profile=profile, duration_s=scout_payload["duration_s"])

    read = _experiment_read_timeout(scout_payload["latency_ms"], scout_payload["jitter_ms"])
    try:
        r = requests.post(f"{base}/agent/experiment/network", json=scout_payload,
                          timeout=(EXPERIMENT_CONNECT_TIMEOUT, read))
        r.raise_for_status()
        data = r.json() if r.content else {}
    except requests.RequestException as exc:
        _record_experiment_history(
            vehicle_id=vid, action="apply_failed", result="scout_unreachable",
            experiment_id=experiment_id, direction="scout_to_operator", profile=profile,
            duration_s=scout_payload["duration_s"], detail={"error": str(exc)},
            severity="warning")
        return JSONResponse(status_code=502, content=_stable_experiment_state(
            vid, status="unavailable", active=False, available=False,
            experiment_id=experiment_id, direction="scout_to_operator", profile=profile,
            error="Scout experiment controller did not acknowledge — poll for confirmed state"))

    state = _normalize_scout_experiment(vid, data)
    if not state.get("experiment_id"):
        state["experiment_id"] = experiment_id   # carry ours if Scout echoed none
    _observe_experiment_state(vid, state)
    return state


@app.delete("/api/experiment/network")
def stop_network_experiment(vehicle_id: Optional[str] = None):
    """Stop / clear the active impairment. Idempotent and safe when nothing is active. Proxies
    to Scout and returns Scout-confirmed state — never an optimistic inactive before Scout
    confirms it. A confirmed stop is recorded as a manual stop in experiment history."""
    global _last_experiment_vehicle_id
    vid = _experiment_vehicle_id(vehicle_id)
    base = vehicle_api_base(vid)
    if base is None:
        return JSONResponse(status_code=409, content=_stable_experiment_state(
            vid, status="unavailable", active=False, available=False,
            error="No Scout experiment controller configured for this vehicle"))
    _last_experiment_vehicle_id = vid
    t = _experiment_tracker(vid)
    prof = t.get("profile") or {}
    read = _experiment_read_timeout(prof.get("latency_ms"), prof.get("jitter_ms"))
    stopped_id = t.get("experiment_id")
    try:
        r = requests.delete(f"{base}/agent/experiment/network",
                            timeout=(EXPERIMENT_CONNECT_TIMEOUT, read))
        r.raise_for_status()
        data = r.json() if r.content else {}
    except requests.RequestException as exc:
        _record_experiment_history(
            vehicle_id=vid, action="apply_failed", result="scout_unreachable",
            experiment_id=stopped_id, detail={"error": str(exc), "op": "stop"},
            severity="warning")
        return JSONResponse(status_code=503, content=_stable_experiment_state(
            vid, status="unavailable", active=False, available=False,
            error="Scout experiment controller unreachable"))

    state = _normalize_scout_experiment(vid, data)
    if state.get("active") is True:
        # Scout says it is still active — do NOT fabricate an inactive result; reflect truth.
        _observe_experiment_state(vid, state)
        return state

    # Scout confirms inactive. Record a manual stop only when there was actually something to
    # stop (a known active experiment, or one Scout named), and mark it terminal so a following
    # GET does not ALSO log an expiry for the same experiment. A repeated DELETE with nothing
    # active is harmless and quiet.
    rec_id = stopped_id or state.get("experiment_id")
    if rec_id and ("terminal", rec_id) not in t["recorded"]:
        t["recorded"].add(("terminal", rec_id))
        _record_experiment_history(
            vehicle_id=vid, action="stopped_manually", result="stopped",
            experiment_id=rec_id, direction=t.get("direction"), profile=prof or None)
    t["active"] = False
    t["experiment_id"] = None
    t["profile"] = {}
    return state


@app.get("/api/experiment/network/history")
def get_experiment_history(limit: int = 200):
    """Durable-within-process experiment history (requested / confirmed / rejected / apply
    failed / stopped / expired / unreachable). The proposed durable-log endpoint from
    BACKEND_ROADMAP — chronological, capped to the most recent `limit`."""
    now = datetime.now(timezone.utc)
    items = experiment_history[-limit:] if limit and limit > 0 else list(experiment_history)
    return {"history": items, "count": len(experiment_history), "generated_at": now.isoformat()}


# ── Replanning supervisory API (Scout Local Agent /agent/replan/*, port 8090) ─────────
# The Operator Station is a THIN PROXY over Scout's replanning controller (see scout_replan.py
# for the outcome model, replan_package.py for planning-package construction). It constructs
# the approved planning package, issues explicit supervisory operations, and presents Scout's
# status accurately — it recreates NONE of Scout's FSM, energy decision or execution state.
#
# Every WRITE is recorded in an in-memory operation log (accepted / rejected / unknown), so an
# operation whose HTTP response was lost is preserved as UNKNOWN and reconciled by a later GET
# rather than silently retried. Writes only ever happen on an explicit operator route call —
# never during polling — so reconnect/poll can never resend a package/config/injection.
MAX_REPLAN_OPERATIONS = 2000
replan_operations = []   # [ {seq, vehicle_id, vehicle, operation, requested_at, http_status,
                         #    outcome, scout_error_code, mission_id, transition_id, ...} ]
_replan_op_seq = 0


def _scout_field(result, *names):
    """First present value among `names` in a scout_replan result's Scout body (or None)."""
    body = result.get("scout") if isinstance(result, dict) else None
    if not isinstance(body, dict):
        return None
    for n in names:
        if body.get(n) is not None:
            return body.get(n)
    return None


def _record_replan_operation(vid, result, *, mission_id=None):
    """Append one write to the operation trace: operation type, target USV, requested-at,
    response status, accepted/rejected/unknown outcome, Scout error code, and the mission /
    transition id where Scout supplies one. Read-only reconnaissance (GETs) is not logged —
    only writes, which are the operations reconnect must never duplicate."""
    global _replan_op_seq
    _replan_op_seq += 1
    entry = {
        "seq": _replan_op_seq,
        "vehicle_id": vehicle_slug(vid),
        "vehicle": name_of(vid),
        "operation": result.get("operation"),
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "http_status": result.get("http_status"),
        "outcome": result.get("outcome"),
        "scout_error_code": result.get("scout_error_code"),
        "mission_id": mission_id or _scout_field(result, "mission_id"),
        "transition_id": _scout_field(result, "transition_id"),
        "revision": _scout_field(result, "revision"),
        "supported": result.get("supported", True),
        "simulated": bool(_scout_field(result, "simulated") or
                          (_scout_field(result, "source") == "SIMULATED")),
    }
    replan_operations.append(entry)
    if len(replan_operations) > MAX_REPLAN_OPERATIONS:
        del replan_operations[:len(replan_operations) - MAX_REPLAN_OPERATIONS]
    # Surface accepted/rejected writes and every unknown outcome on the operator event log too.
    sev = ("warning" if entry["outcome"] == scout_replan.OUTCOME_UNKNOWN
           else "caution" if entry["outcome"] == scout_replan.OUTCOME_REJECTED else "info")
    _append_event(severity=sev, etype="replan-operation", source="operator-backend",
                  vehicle_id=vid, vehicle=name_of(vid),
                  message=f"Replan {entry['operation']} -> {entry['outcome']}"
                          + (f" ({entry['scout_error_code']})" if entry["scout_error_code"] else ""),
                  detail=entry)
    return entry


# ── Bounded Pixhawk read-back evidence for polling (readiness) ────────────────────────
# A full `GET /agent/pixhawk_mission` makes Scout download the entire mission from the flight
# controller over MAVLink. Readiness is POLLED (the Agent page refreshes it every 2.5 s), so
# calling it unconditionally would mean a continuous mission download per open browser tab for
# as long as the page is open — expensive on the vehicle, and it buys nothing, because an
# immutable mission's read-back does not change between two polls seconds apart.
#
# So routine polling reads through this short-TTL cache and LABELS the evidence with its age;
# it never presents stale evidence as fresh. An explicit operation that must decide something
# (the planning-package sync, which gates on the read-back matching the approved hash) passes
# max_age_s=0 and always pays for a live download.
PIXHAWK_READBACK_TTL_S = 10.0
_pixhawk_readback_cache = {}     # {vid: (fetched_at: datetime, result: dict)}


def _pixhawk_readback(vid, base, now, *, max_age_s=PIXHAWK_READBACK_TTL_S):
    """One Pixhawk read-back for `vid`, reusing cached evidence younger than `max_age_s`.

    Always returns the normalized `_scout_mission_read` dict plus two honesty fields:
      `evidence_age_s`  how old this read-back is, in seconds (0.0 for a fresh download)
      `evidence_cached` whether it was served from the cache rather than downloaded now
    so a caller can never mistake a cached read-back for a live one. `max_age_s=0` forces a
    live download. Unreachable results are cached too — a Scout that is down should not be
    re-polled at full rate either, and `reachable:false` is itself the answer."""
    entry = _pixhawk_readback_cache.get(vid)
    if entry is not None and max_age_s > 0:
        fetched_at, cached = entry
        age = (now - fetched_at).total_seconds()
        if 0 <= age <= max_age_s:
            out = dict(cached)
            out["evidence_age_s"] = round(age, 3)
            out["evidence_cached"] = True
            return out
    result = _scout_mission_read(vid, base, now)
    _pixhawk_readback_cache[vid] = (now, result)
    out = dict(result)
    out["evidence_age_s"] = 0.0
    out["evidence_cached"] = False
    return out


def _first_present(*values):
    """The first non-None value, or None. Distinguishes "absent" from a legitimate False."""
    for v in values:
        if v is not None:
            return v
    return None


def _as_dict(value):
    return value if isinstance(value, dict) else {}


def _normalize_scout_package(scout, legacy_geometry=None):
    """Flatten Scout's planning-package GET body into the ONE evidence shape the operator
    reasons with, for BOTH the replan-planning-package-v1 response and the pre-v1 flat one.

    Scout's v1 GET nests its evidence:

        { stored, usable, package:{…}, summary:{…}, envelope:{…}, readiness:{…} }

    while the older Scout put mission_id / route hash / counts / geometry_validation directly
    on the response. Reading only the flat shape against a v1 Scout is what made a stored,
    usable package report `mission_id: null` and `route_hash: null`, which then blanked the
    hash comparison and held REPLANNING READY false against a package Scout itself called
    ready. Precedence here is explicit — nested first, flat as the legacy fallback — so
    neither Scout generation silently loses a field.

    This is a pure reader: it extracts and labels Scout's evidence and decides NOTHING. The
    operator-side comparisons (mission id match, the route-hash chain, consistency, readiness)
    are computed by the caller, which owns the active mission record Scout cannot see.
    """
    scout = _as_dict(scout)
    package = _as_dict(scout.get("package"))
    summary = _as_dict(scout.get("summary"))
    scout_readiness = _as_dict(scout.get("readiness"))
    geometry = _as_dict(legacy_geometry)

    mission_id = _first_present(package.get("mission_id"), summary.get("mission_id"),
                                scout.get("mission_id"))
    route_hash = _first_present(
        package.get("route_hash"), package.get("original_route_hash"),
        package.get("route_content_hash"),
        summary.get("route_hash"), summary.get("original_route_hash"),
        summary.get("route_content_hash"),
        # legacy flat response (a pre-v1 Scout names the same value route_content_hash)
        scout.get("route_hash"), scout.get("original_route_hash"),
        scout.get("route_content_hash"))

    route_count = summary.get("route_waypoint_count")
    if route_count is None:
        for key in ("route_waypoints", "route"):
            seq = package.get(key)
            if isinstance(seq, (list, tuple)):
                route_count = len(seq)
                break
    if route_count is None:
        route_count = _first_present(scout.get("route_waypoint_count"), scout.get("route_count"))

    # Geometry evidence. The v1 summary says WHAT the package carries; the v1 readiness block
    # says what Scout CHECKED. They are reported apart, because carrying geometry and having
    # validated it are different claims.
    has_geometry = summary.get("has_navigable_geometry")
    has_boundary = summary.get("has_navigable_boundary")
    if has_geometry is None and has_boundary is None:
        if "navigable_geometry" in package:
            boundary_available = bool(package.get("navigable_geometry"))
        else:
            boundary_available = geometry.get("boundary_available")
    else:
        boundary_available = bool(has_geometry or has_boundary)
    boundary_checked = _first_present(scout_readiness.get("navigable_geometry_checked"),
                                      geometry.get("boundary_checked"))

    # An EMPTY no-go set is an answer, not an absence: a package that explicitly carries zero
    # no-go zones has available evidence (Scout looked and reported none), so availability is
    # keyed on the field being reported at all — never on the zone count being non-zero.
    if (summary.get("no_go_zones_present") is not None
            or summary.get("no_go_zone_count") is not None
            or "no_go_zones" in package):
        no_go_available = True
    else:
        no_go_available = geometry.get("no_go_available")
    no_go_checked = _first_present(scout_readiness.get("no_go_zones_checked"),
                                   geometry.get("no_go_checked"))

    # HOW MANY zones, not just whether the field exists. Availability answers "did Scout report
    # on no-go zones at all"; the COUNT answers "is Scout planning against the obstacle this
    # experiment is built around". They are different questions, and only the second one catches
    # the configuration that reports no_go_zones_present=true with zero zones in the package.
    # Scout already sends this in its v1 summary — it was simply being dropped here.
    no_go_zone_count = summary.get("no_go_zone_count")
    if no_go_zone_count is None and isinstance(package.get("no_go_zones"), list):
        no_go_zone_count = len(package["no_go_zones"])
    no_go_zones_present = summary.get("no_go_zones_present")
    if no_go_zones_present is None and no_go_zone_count is not None:
        no_go_zones_present = bool(no_go_zone_count)

    # connector_proven_safe is TRI-state and stays tri-state: null means Scout did not prove
    # the connector safe either way. It is never widened to true.
    if "connector_proven_safe" in scout_readiness:
        connector_proven_safe = scout_readiness.get("connector_proven_safe")
    else:
        connector_proven_safe = geometry.get("connector_proven_safe")

    return {
        "stored": _first_present(scout.get("stored"), summary.get("stored"),
                                 package.get("stored")),
        "usable": _first_present(scout.get("usable"), summary.get("usable")),
        "mission_id": mission_id,
        "route_hash": route_hash,
        "route_count": route_count,
        "boundary_available": boundary_available,
        "boundary_checked": boundary_checked,
        "no_go_available": no_go_available,
        "no_go_checked": no_go_checked,
        "no_go_zone_count": no_go_zone_count,
        "no_go_zones_present": no_go_zones_present,
        "connector_proven_safe": connector_proven_safe,
        "shoreline_clearance_available": geometry.get("shoreline_clearance_available"),
        # Scout's own verdicts, echoed rather than re-derived. `mission_id_consistent` is
        # deliberately NOT used as a gate: Scout cannot compare mission ids when the Pixhawk
        # read-back exposes none, so it honestly reports null — the operator, which owns the
        # active mission record, performs that comparison itself.
        "scout_replanning_ready": scout_readiness.get("replanning_ready"),
        "scout_state": scout_readiness.get("state"),
        "scout_mission_verified": scout_readiness.get("mission_verified"),
        "scout_route_hash_match": scout_readiness.get("route_hash_match"),
        "scout_mission_id_consistent": scout_readiness.get("mission_id_consistent"),
    }


# ── Startup / reconnect reconciliation ────────────────────────────────────────────────
# The verdict is RUNTIME EVIDENCE and is deliberately NOT persisted: it describes a comparison
# between the durable record and live Scout/Pixhawk state at one instant, and a restored copy of
# it would be a fabricated observation of a vehicle nobody has spoken to yet. What IS persisted
# is the thing reconciliation repairs — approved mission identity, its verified upload status and
# its package synchronization state. The verdict is recomputed from fresh evidence every time.
#
# Keyed per vehicle. One vehicle's reconciliation can never read, write or select another
# vehicle's records: mission_reconcile filters the candidate list by vehicle id and re-checks
# ownership on every record it considers.
_reconciliation_by_vehicle = {}      # {vid: verdict dict} — in-memory, never persisted


def _reconcile_deps():
    """The operator-backend facts reconciliation runs on. Built per call so a test that swaps a
    store sees the swap. Note what is NOT here: no command queue, no MISSION_UPLOAD, no Scout
    write. Reconciliation cannot reach the vehicle even by accident."""
    return mission_reconcile.Deps(
        vehicle_records=lambda v: [r for r in original_missions.values()
                                   if r.get("vehicle_id") == v],
        active_mission_id=lambda v: active_original_by_vehicle.get(v),
        set_active=lambda v, mid: active_original_by_vehicle.__setitem__(v, mid),
        persist=_save_mission_store,
    )


def _reconcile_vehicle_mission(vid, readback, package_evidence, package_reachable):
    """Run reconciliation for one vehicle and remember its verdict. Every state CHANGE is put on
    the operator event log — silently re-pointing which mission a vehicle is flying would be the
    same class of dishonesty as the mismatch this repairs."""
    verdict = mission_reconcile.reconcile(
        _reconcile_deps(), vid, readback=readback,
        package_evidence=package_evidence, package_reachable=package_reachable)
    previous = _reconciliation_by_vehicle.get(vid)
    _reconciliation_by_vehicle[vid] = verdict
    for action in verdict.get("actions") or []:
        _append_event(
            severity="caution" if action["action"] == mission_reconcile.ACTION_REBIND else "info",
            etype="mission-reconcile", source="operator-backend",
            vehicle_id=vid, vehicle=name_of(vid),
            message=f"Mission reconciliation: {action['action']} — {action.get('detail') or ''}".strip(),
            detail=action)
    if previous is None or previous.get("outcome") != verdict.get("outcome"):
        print(f"[MISSION RECONCILE] {vehicle_slug(vid)} -> {verdict['outcome']} "
              f"({verdict['reason']}); active={verdict.get('active_mission_id')}")
    return verdict


def reconciliation_for(vid):
    """The last reconciliation verdict for a vehicle, or a RECONCILING placeholder when none has
    been computed yet in this process. Never invents a verdict: 'no evidence has arrived since
    this backend started' is exactly what a fresh station should say, and it is the honest answer
    that keeps a startup from rendering as a mismatch."""
    verdict = _reconciliation_by_vehicle.get(vid)
    if verdict is not None:
        return verdict
    return {
        "outcome": mission_reconcile.RECONCILING,
        "conclusive": False,
        "reason": "NO_EVIDENCE_YET",
        "detail": "No mission evidence has been read from this vehicle since the station "
                  "started, so the approved mission has not been reconciled yet.",
        "generated_at": None, "actions": [], "rebound": False,
        "active_mission_id": active_original_by_vehicle.get(vid),
        "active_route_hash": (original_missions.get(active_original_by_vehicle.get(vid)) or {})
                             .get("route_hash"),
        "evidence": {},
    }


def _compute_replan_readiness(vid, base, *, max_readback_age_s=PIXHAWK_READBACK_TTL_S):
    """The combined readiness summary (task Section 3). Keeps the Vehicle mission and the Scout
    planning package as two DISTINCT operations, never letting a successful Pixhawk upload hide
    a package failure, and reports limitations separately. Returns a JSON-able dict.

    The package verdict is computed from evidence, not copied from Scout:

        mission_id_match  package mission id == the operator's ACTIVE mission id
        hash_match        package route hash == the record's route hash == the Pixhawk's
        consistent        stored and usable and mission_id_match and hash_match
        replanning_ready  mission_ready and consistent and Scout's own readiness verdict

    The operator makes the mission-id comparison itself because Scout cannot: the Pixhawk
    mission read-back carries no operator mission id, so Scout reports `mission_id_consistent:
    null` — an honest "cannot compare", never a failure. The operator owns the active mission
    record, so it can and does compare.

    `max_readback_age_s` bounds how old the Pixhawk read-back evidence may be. The default is the
    age-labelled polling cache; a caller that is about to AUTHORIZE A VEHICLE WRITE passes 0 and
    pays for a live MAVLink download, because a ten-second-old hash is evidence about the past.
    The Start transaction is the caller that does that (mission_lifecycle.run_start)."""
    now = datetime.now(timezone.utc)

    # A. Vehicle mission — the immutable revision-0 record + a BOUNDED Pixhawk read-back.
    # Display paths go through the age-labelled cache rather than forcing a fresh mission
    # download on every refresh; max_readback_age_s=0 forces a live one (see _pixhawk_readback).
    flask_base = vehicle_api_base(vid)
    readback = (_pixhawk_readback(vid, flask_base, now, max_age_s=max_readback_age_s)
                if flask_base else None)

    # B. Scout planning package — live status + package pull (never fabricated).
    # Read BEFORE the comparisons below, because reconciliation needs both halves of the
    # evidence and its result decides WHICH record the comparisons are made against.
    status = scout_replan.get_status(base)
    pkg = scout_replan.get_planning_package(base)
    scout_reachable = bool(status.get("reachable") and status.get("supported"))
    consistency = (_scout_field(status, "package_consistency")
                   or _scout_field(pkg, "consistency", "package_consistency"))
    geometry = (_scout_field(status, "geometry_validation")
                or _scout_field(pkg, "geometry_validation") or {})
    if not isinstance(geometry, dict):
        geometry = {}
    # Scout's package evidence, read through ONE normalizer that understands both the v1
    # nested response and the pre-v1 flat one (see _normalize_scout_package).
    ev = _normalize_scout_package(pkg.get("scout"), geometry)

    # ── Reconciliation ────────────────────────────────────────────────────────────────────
    # Re-identify which APPROVED record this vehicle is actually carrying, from the evidence
    # just gathered, and repair the operator's own durable bookkeeping when it can be PROVEN
    # wrong. This is what stops a restored active pointer at a superseded record from reporting
    # a package mismatch against a mission the store already holds. It performs no vehicle
    # command and no mission upload (mission_reconcile.py), and it decides nothing at all when
    # the evidence is incomplete.
    reconciliation = _reconcile_vehicle_mission(vid, readback, ev, scout_reachable)

    mission_id = active_original_by_vehicle.get(vid)
    rec = original_missions.get(mission_id) if mission_id else None

    record_hash = rec.get("route_hash") if rec else None
    upload_status = rec.get("upload_status") if rec else None
    readback_hash = readback.get("route_content_hash") if isinstance(readback, dict) else None
    pixhawk_verified = bool(rec and upload_status == "VERIFIED")
    readback_hash_match = bool(record_hash and readback_hash and record_hash == readback_hash)
    home, home_source = _current_home_for_package(vid, rec) if rec else (None, None)
    home_valid = home is not None
    boundary_supplied = bool(rec and (rec.get("navigable_geometry")
                             or (rec.get("planning_inputs") or {}).get("navigable_boundary")))

    vehicle_mission = {
        "mission_id": mission_id, "record_present": rec is not None,
        "route_hash": record_hash, "upload_status": upload_status,
        "pixhawk_verified": pixhawk_verified,
        "readback_reachable": bool(readback and readback.get("reachable")),
        "readback_hash": readback_hash, "readback_hash_match": readback_hash_match,
        "readback_partial": bool(readback.get("partial")) if readback else None,
        # How old this read-back evidence is. Polling reuses a bounded cache, so the age is
        # reported rather than implied — a cached read-back is never shown as a live one.
        "readback_age_s": readback.get("evidence_age_s") if readback else None,
        "readback_cached": readback.get("evidence_cached") if readback else None,
        # Passed through only when Scout's read-back carried them (never fabricated). Additive
        # fields for mission_full_refresh.py's pixhawk-evidence view — no existing consumer read
        # these off vehicle_mission before, so nothing here can regress an existing comparison.
        "readback_route_count": readback.get("route_waypoint_count") if readback else None,
        "readback_current_seq": readback.get("current_seq") if readback else None,
        "home_valid": home_valid, "home_source": home_source,
        "boundary_supplied": boundary_supplied,
    }

    package_mission_id = ev["mission_id"] or _scout_field(status, "mission_id")
    package_hash = ev["route_hash"] or _scout_field(status, "route_content_hash")
    stored_flag = ev["stored"]
    package_stored = (bool(stored_flag) if stored_flag is not None
                      else bool(package_mission_id or package_hash))
    if ev["usable"] is not None:
        package_usable = bool(ev["usable"])        # Scout states it outright in v1
    else:
        package_usable = consistency not in ("PLANNING_PACKAGE_MISSING", "PLANNING_PACKAGE_UNUSABLE", None) \
            or (package_stored and consistency is None and scout_reachable)
    consistency_ok = consistency == PACKAGE_CONSISTENT

    # The three comparisons the OPERATOR owns. Scout cannot make the mission-id one at all
    # (the Pixhawk read-back carries no operator mission id, so Scout reports null), and it
    # cannot see the operator's immutable record — so a null from Scout is never read as a
    # failure here; the operator compares against its own active mission instead.
    mission_id_match = bool(package_mission_id and mission_id and package_mission_id == mission_id)
    hash_available = bool(package_hash and record_hash)
    # The full chain: the package's route == the approved route == the route on the Pixhawk.
    hash_match = bool(package_hash and record_hash and package_hash == record_hash
                      and readback_hash_match)
    hash_mismatch = bool(record_hash and package_hash and record_hash != package_hash)
    package_consistent = bool(package_stored and package_usable
                              and mission_id_match and hash_match)

    planning_package = {
        "scout_reachable": scout_reachable,
        "scout_supported": bool(status.get("supported")),
        "stored": package_stored, "usable": package_usable,
        "mission_id": package_mission_id, "mission_id_match": mission_id_match,
        "route_hash": package_hash, "hash_match": hash_match, "hash_mismatch": hash_mismatch,
        "hash_comparison_available": hash_available,
        "consistency": consistency, "consistent": package_consistent,
        "scout_consistency_ok": consistency_ok,
        "route_count": ev["route_count"],
        "boundary_available": ev["boundary_available"],
        "boundary_checked": ev["boundary_checked"],
        "no_go_available": ev["no_go_available"],
        "no_go_checked": ev["no_go_checked"],
        # The count Scout's package summary reports, carried through so the operator can compare
        # it against the approved record's own no-go count (E2 preflight). Null when Scout does
        # not report one — never defaulted to 0, which would read as "no obstacle".
        "no_go_zone_count": ev["no_go_zone_count"],
        "no_go_zones_present": ev["no_go_zones_present"],
        "connector_proven_safe": ev["connector_proven_safe"],
        "shoreline_clearance_available": ev["shoreline_clearance_available"],
        # Scout's own readiness verdict, echoed verbatim — including the mission-id comparison
        # it honestly cannot make.
        "scout_replanning_ready": ev["scout_replanning_ready"],
        "scout_state": ev["scout_state"],
        "scout_mission_verified": ev["scout_mission_verified"],
        "scout_route_hash_match": ev["scout_route_hash_match"],
        "scout_mission_id_consistent": ev["scout_mission_id_consistent"],
    }

    # Scout's own readiness gate. A v1 Scout states it directly; a pre-v1 Scout has no
    # readiness block, and there its package-consistency verdict is the equivalent gate — so
    # an older Scout is not held un-ready for a field it never shipped.
    scout_ready_flag = ev["scout_replanning_ready"]
    scout_ready_ok = consistency_ok if scout_ready_flag is None else bool(scout_ready_flag)

    # Limitations — reported SEPARATELY from readiness, never as a silent pass.
    limitations = []
    if not boundary_supplied:
        limitations.append("Navigable boundary absent — connector cannot be proven safe")
    if ev["connector_proven_safe"] is False:
        limitations.append("Scout could not prove the current-position connector safe")
    if hash_mismatch:
        limitations.append("Package route hash does NOT match the approved route — replanning blocked")
    if not hash_available and scout_reachable:
        limitations.append("Scout did not report a package route hash — hash comparison unavailable")
    if hash_available and not readback_hash_match:
        limitations.append("Pixhawk read-back hash does not confirm the approved route — the "
                           "package/record/flight-controller hash chain is unproven")
    if scout_ready_flag is False:
        limitations.append("Scout reports its own replanning readiness as false"
                           + (f" (state {ev['scout_state']})" if ev["scout_state"] else ""))
    limitations.append("shoreline_clearance_m is scalar metadata, not geometry Scout can "
                       "run an onboard clearance check against")
    for lim in (geometry.get("limitations") or []):
        if isinstance(lim, str) and lim not in limitations:
            limitations.append(lim)

    mission_ready = bool(pixhawk_verified and home_valid)
    replanning_ready = bool(mission_ready and package_consistent and scout_ready_ok)

    return {
        "ok": True, "vehicle_id": vehicle_slug(vid), "generated_at": now.isoformat(),
        "mission_ready": mission_ready, "replanning_ready": replanning_ready,
        "vehicle_mission": vehicle_mission, "planning_package": planning_package,
        # What the operator's own bookkeeping was reconciled to, and on what evidence. Carried
        # so the UI can say "still establishing which mission this is" instead of "mismatch"
        # while the comparison is genuinely incomplete.
        "reconciliation": reconciliation,
        "limitations": limitations,
    }


def _local_agent_target(vehicle_id, subsystem):
    """(vid, base) for ANY Local Agent (port 8090) route, or a JSONResponse to return directly.

    Three honest cases, mirroring the Pixhawk/authority proxies:
      unknown vehicle id            → 404 (no such vehicle)
      known, no Local Agent route   → 200 supported:false (nothing to talk to here)
      known, Local Agent configured → (vid, base) for the live proxy.

    The base URL comes from local_agent_base(), so a route ALWAYS targets the vehicle in the
    path and one vehicle's Local Agent is never substituted for another's."""
    vid = parse_vehicle_id(vehicle_id)
    if vid not in known_vehicle_ids():
        return None, JSONResponse(status_code=404, content={
            "ok": False, "error": "unknown vehicle", "vehicle_id": vehicle_id})
    base = local_agent_base(vid)
    if base is None:
        return None, JSONResponse(status_code=200, content={
            "ok": False, "supported": False, "reachable": False,
            "vehicle_id": vehicle_slug(vid), "outcome": "unsupported",
            "error": f"No Scout Local Agent {subsystem} API configured for this vehicle"})
    return (vid, base), None


def _replan_target(vehicle_id):
    return _local_agent_target(vehicle_id, "replanning")


def _replan_status_code(result):
    """The honest HTTP status for a scout_replan outcome — non-500 for every handled case so
    the frontend poll never sees a console error for an unreachable/older Scout."""
    outcome = result.get("outcome")
    if outcome == scout_replan.OUTCOME_UNSUPPORTED:
        return 200          # older Scout — a handled, honest "not supported", not an error
    if outcome == scout_replan.OUTCOME_UNAVAILABLE:
        return 503          # reachable:false read — the frontend's honest "unavailable"
    if outcome == scout_replan.OUTCOME_UNKNOWN:
        return 202          # accepted-but-unconfirmed: reconcile with a GET (never a failure)
    if outcome == scout_replan.OUTCOME_REJECTED:
        return 409 if result.get("transaction_active") else 400
    return 200


def _replan_response(vid, result):
    """Normalize a scout_replan result into the operator response envelope (adds vehicle_id
    and keeps Scout's body under `scout`), at the honest HTTP status for its outcome."""
    result = dict(result)
    result["vehicle_id"] = vehicle_slug(vid)
    return JSONResponse(status_code=_replan_status_code(result), content=result)


def _current_home_for_package(vid, record):
    """(home, source) for a planning package: Scout's live verified Home if it is reporting one,
    else the plan's own planning_home from the record. None when neither yields a valid fix —
    a package is never built with a fabricated Home."""
    lk = last_known_agent.get(vid)
    hs = lk.get("home_status") if isinstance(lk, dict) else None
    if isinstance(hs, dict):
        home = replan_package.normalize_home(hs.get("home_position") or hs)
        if home:
            return home, "verified_home"
    ph = (record.get("planning_inputs") or {}).get("planning_home")
    home = replan_package.normalize_home(ph)
    if home:
        return home, "planning_home"
    return None, None


@app.get("/api/vehicles/{vehicle_id}/replan/status")
def replan_status(vehicle_id: str):
    """Scout's canonical replanning status object (the same one carried under agent.replan in
    the normal status push), pulled live from the Local Agent. Read-only; never fabricated."""
    target, err = _replan_target(vehicle_id)
    if err is not None:
        return err
    vid, base = target
    return _replan_response(vid, scout_replan.get_status(base))


@app.get("/api/vehicles/{vehicle_id}/replan/config")
def replan_get_config(vehicle_id: str):
    """Resolved replanning config with the source of each value (default/environment/runtime)."""
    target, err = _replan_target(vehicle_id)
    if err is not None:
        return err
    vid, base = target
    return _replan_response(vid, scout_replan.get_config(base))


@app.patch("/api/vehicles/{vehicle_id}/replan/config")
async def replan_patch_config(vehicle_id: str, request: Request):
    """Patch runtime config (Scout's patchable fields only). A 409 TRANSACTION_ACTIVE is
    surfaced as a distinct rejection, never a generic network failure. Recorded as a write."""
    target, err = _replan_target(vehicle_id)
    if err is not None:
        return err
    vid, base = target
    try:
        body = await request.json()
    except Exception:
        body = {}
    patch = {k: v for k, v in (body or {}).items() if k in REPLAN_PATCHABLE_FIELDS}
    if not patch:
        return JSONResponse(status_code=400, content={
            "ok": False, "vehicle_id": vehicle_slug(vid), "error": "no patchable fields supplied",
            "patchable_fields": sorted(REPLAN_PATCHABLE_FIELDS)})
    result = scout_replan.patch_config(base, patch)
    _record_replan_operation(vid, result)
    return _replan_response(vid, result)


@app.get("/api/vehicles/{vehicle_id}/replan/planning-package")
def replan_get_package(vehicle_id: str):
    """The planning package currently stored on Scout (single slot), for reconciliation."""
    target, err = _replan_target(vehicle_id)
    if err is not None:
        return err
    vid, base = target
    return _replan_response(vid, scout_replan.get_planning_package(base))


@app.put("/api/vehicles/{vehicle_id}/replan/planning-package")
async def replan_put_package(vehicle_id: str, request: Request):
    """Construct the approved planning package from a stored immutable mission record and PUT it
    to Scout. Body: { mission_id? } — defaults to the vehicle's active original mission. The
    route bytes are the record's Pixhawk route (hash-invariant); Scout re-validates and returns
    package consistency. Idempotent: the same PUT replaces Scout's single slot, never adds one."""
    target, err = _replan_target(vehicle_id)
    if err is not None:
        return err
    vid, base = target
    try:
        body = await request.json()
    except Exception:
        body = {}
    mission_id = (body or {}).get("mission_id") or active_original_by_vehicle.get(vid)
    rec = original_missions.get(mission_id) if mission_id else None
    if rec is None:
        return JSONResponse(status_code=404, content={
            "ok": False, "vehicle_id": vehicle_slug(vid),
            "error": "no mission record to package",
            "detail": "Finalize a survey mission for this vehicle first (revision 0 record)."})
    if rec.get("vehicle_id") != vid:
        return JSONResponse(status_code=400, content={
            "ok": False, "vehicle_id": vehicle_slug(vid), "error": "mission belongs to another vehicle",
            "mission_vehicle_id": vehicle_slug(rec.get("vehicle_id"))})
    home, home_source = _current_home_for_package(vid, rec)
    try:
        package, meta = replan_package.build_package(
            rec, home, usv_id=vehicle_slug(vid))
    except replan_package.PackageError as exc:
        return JSONResponse(status_code=400, content={
            "ok": False, "vehicle_id": vehicle_slug(vid),
            "error": "planning_package_unbuildable", "message": str(exc)})
    result = scout_replan.put_planning_package(base, package)
    _record_replan_operation(vid, result, mission_id=mission_id)
    # The honest outcome envelope, plus the operator-side package metadata (what was sent:
    # hash, labels, limitations) so the UI shows it alongside Scout's verdict.
    envelope = dict(result)
    envelope["vehicle_id"] = vehicle_slug(vid)
    envelope["operator_package"] = {
        "mission_id": mission_id, "home_source": home_source,
        "route_content_hash": meta["route_content_hash"],
        "waypoint_count": meta["waypoint_count"],
        "segment_label_counts": meta["segment_label_counts"],
        "boundary_supplied": meta["boundary_supplied"],
        "no_go_supplied": meta["no_go_supplied"],
        "limitations": meta["limitations"],
    }
    return JSONResponse(status_code=_replan_status_code(result), content=envelope)


# ══════════════════════════════════════════════════════════════════════════════════════════
# THE PUBLISH TRANSACTION — mission upload → Operator record → Scout planning package
# ══════════════════════════════════════════════════════════════════════════════════════════
# Finalizing a survey used to stop at the flight controller: the record was stored, the
# MISSION_UPLOAD was queued and verified, and the Plan page said "Uploaded & verified" — while
# Scout still held the PREVIOUS mission's planning package. Nothing in the station ever sent the
# new one (`syncReplanPackage` had zero call sites; the Agent page's package button POSTed the
# older pre-v1 shape), so the next Start correctly refused with a mission/package mismatch that
# only a manual curl could clear.
#
# mission_publish.py is now the ONE place that completes the publication, and both entry points
# below run the same function over the same evidence:
#
#   POST /api/vehicles/{id}/missions/publish                 the full transaction (Plan page)
#   POST /api/vehicles/{id}/replan/planning-package/sync      package-only retry (unchanged URL)
#
# Neither issues a vehicle command. The Pixhawk write remains the at-least-once MISSION_UPLOAD
# command queue, which is why publish is RESUMABLE rather than blocking: while that command is
# in flight it answers UPLOADING_PIXHAWK / 202, and the caller invokes it again once the
# read-back verification lands.
MAX_PUBLISH_OPERATIONS = 200
publish_operations = []          # the publish trace, newest last (diagnostics)


def _record_publish_operation(entry):
    publish_operations.append(entry)
    if len(publish_operations) > MAX_PUBLISH_OPERATIONS:
        del publish_operations[:len(publish_operations) - MAX_PUBLISH_OPERATIONS]
    sev = "info" if entry.get("agent_ready") else "caution"
    _append_event(severity=sev, etype="mission-publish", source="operator-backend",
                  vehicle_id=parse_vehicle_id(entry.get("vehicle_id")),
                  # ASCII "->" on purpose — this message is echoed to the console/log, and the
                  # Windows locale encoding a redirected stdout uses cannot encode "→".
                  message=f"Publish {entry.get('operation')} -> {entry.get('state')}"
                          + (f" ({entry['error']})" if entry.get("error") else ""),
                  detail=entry)


def _publish_deps():
    """The operator-backend facts the publish transaction runs on. Built per request so a test
    that swaps a store or a transport sees the swap.

    `pixhawk_readback` passes max_age_s=0 UNCONDITIONALLY: this transaction decides whether a
    package may be sent, and a ten-second-old hash is evidence about the past, not a proof that
    the flight controller carries the approved route right now."""
    return mission_publish.Deps(
        active_mission_id=lambda vid: active_original_by_vehicle.get(vid),
        mission_record=lambda mid: original_missions.get(mid),
        pixhawk_readback=lambda vid: (
            _pixhawk_readback(vid, vehicle_api_base(vid), datetime.now(timezone.utc), max_age_s=0)
            if vehicle_api_base(vid) else None),
        scout_get_package=scout_replan.get_planning_package,
        scout_post_package=scout_replan.post_planning_package,
        scout_package_evidence=_normalize_scout_package,
        readiness=lambda vid, base: _compute_replan_readiness(vid, base),
        persist_sync_state=lambda rec: _save_mission_store(),
        record_operation=_record_publish_operation,
    )


# The publish error codes, projected onto the stage/code vocabulary the /sync route has always
# answered with. Kept as an explicit table rather than a lowercase() of the new code so the
# older contract stays a deliberate, reviewable mapping instead of an accident of formatting.
_SYNC_LEGACY = {
    mission_publish.NO_MISSION_RECORD: ("mission_record", "no_mission_record"),
    mission_publish.MISSION_BELONGS_TO_ANOTHER_VEHICLE:
        ("mission_record", "mission_belongs_to_another_vehicle"),
    mission_publish.MISSION_ID_MISMATCH: ("mission_record", "mission_id_mismatch"),
    mission_publish.MISSION_RECORD_ALTERED: ("mission_record", "mission_record_altered"),
    mission_publish.PIXHAWK_UPLOAD_PENDING: ("upload_status", "mission_not_verified"),
    mission_publish.PIXHAWK_UPLOAD_FAILED: ("upload_status", "mission_not_verified"),
    mission_publish.PIXHAWK_READBACK_UNREACHABLE: ("pixhawk_readback", "readback_unreachable"),
    mission_publish.PIXHAWK_READBACK_PARTIAL: ("pixhawk_readback", "readback_partial"),
    mission_publish.PIXHAWK_READBACK_HASH_UNAVAILABLE: ("hash_match", "readback_hash_unavailable"),
    mission_publish.PIXHAWK_HASH_MISMATCH: ("hash_match", "route_hash_mismatch"),
    mission_publish.PIXHAWK_COUNT_MISMATCH: ("hash_match", "route_count_mismatch"),
    mission_publish.OPERATOR_PERSIST_FAILED: ("mission_record", "stale_active_mission"),
    mission_publish.PACKAGE_BUILD_FAILED: ("package_build", "planning_package_unbuildable"),
    mission_publish.SCOUT_UNREACHABLE: ("scout_post", "scout_unreachable"),
    mission_publish.SCOUT_PACKAGE_POST_FAILED: ("scout_post", "scout_post_failed"),
    mission_publish.SCOUT_PACKAGE_READBACK_FAILED: ("scout_package", "package_readback_failed"),
    mission_publish.SCOUT_PACKAGE_NOT_STORED: ("scout_package", "package_not_stored"),
    mission_publish.SCOUT_PACKAGE_ID_MISMATCH: ("scout_package", "package_mission_id_mismatch"),
    mission_publish.SCOUT_PACKAGE_HASH_MISMATCH: ("scout_package", "package_route_hash_mismatch"),
    mission_publish.SCOUT_PACKAGE_COUNT_MISMATCH: ("scout_package", "package_route_count_mismatch"),
    mission_publish.PUBLISH_BUSY: ("busy", "publish_busy"),
}


def _sync_response(vid, base, env):
    """A publish envelope, answered in the /sync route's long-standing response shape.

    The route keeps the fields it has always returned — `synced`, `failed_stage`, the lowercase
    `error` code, `package_sent`, `scout_post`, `scout_package`, the two bracketing read-backs,
    `route_unchanged_across_write`, `readiness` — because scripts and tests read them. Everything
    the new transaction adds (phases, the specific error code, the three-way final comparison,
    `agent_ready`) rides ALONGSIDE them rather than replacing them.

    `synced` deliberately still means only "Scout accepted the POST". Whether the package Scout
    now holds actually matches the approved mission is the separate, stronger `agent_ready`.
    """
    code = env.get("error")
    stage, legacy = _SYNC_LEGACY.get(code, (None, code))
    post = env.get("scout_post")
    accepted = bool(post and post.get("outcome") == scout_replan.OUTCOME_ACCEPTED)
    final = env.get("final") or {}

    readback_before = env.get("pixhawk_readback")
    readback_after = None
    route_unchanged = None
    if post is not None and readback_before is not None:
        # The consistency bracket: a SECOND live read-back after the write proves the route on
        # the flight controller did not change across it. Paid only when a write was attempted.
        flask_base = vehicle_api_base(vid)
        if flask_base:
            readback_after = _pixhawk_readback(vid, flask_base, datetime.now(timezone.utc),
                                               max_age_s=0)
            route_unchanged = bool(readback_after.get("reachable")
                                   and not readback_after.get("partial")
                                   and readback_after.get("route_content_hash")
                                   == env.get("expected_route_hash"))

    body = {
        "ok": accepted, "synced": accepted, "vehicle_id": vehicle_slug(vid),
        "failed_stage": None if accepted else (stage or "scout_post"),
        "error": None if accepted else legacy,
        "message": env.get("message"),
        "generated_at": env.get("generated_at"),
        "mission_id": env.get("mission_id"),
        "route_hash": env.get("expected_route_hash"),
        "upload_status": (env.get("operator_store") or {}).get("upload_status"),
        "operator_package": env.get("operator_package"),
        "package_sent": env.get("package_sent"),
        "scout_post": post,
        "scout_package": env.get("scout_package"),
        "pixhawk_readback_before": readback_before,
        "pixhawk_readback_after": readback_after,
        "route_unchanged_across_write": route_unchanged,
        # Readiness is computed only when a WRITE was actually attempted. A sync refused by its
        # own preconditions has contacted nobody, and paying for a Scout status + package read
        # to decorate that refusal would break the rule that a failed precondition performs no
        # later-stage work (and would make "nothing reached Scout" untestable).
        "readiness": (env.get("readiness") if env.get("readiness") is not None
                      else (_compute_replan_readiness(vid, base) if post is not None else None)),
        # The transaction's own vocabulary, additive.
        "publish": env,
        "state": env.get("state"),
        "phase": env.get("phase"),
        "error_code": code,
        "agent_ready": bool(final.get("agent_ready")),
        "idempotent": bool(env.get("idempotent")),
    }
    # Evidence the older failure responses carried inline, preserved where it applies.
    if env.get("pixhawk_readback") is not None:
        body["pixhawk_readback"] = env["pixhawk_readback"]
        body["readback_hash"] = (env.get("pixhawk") or {}).get("route_hash")
    for ph in env.get("phases") or []:
        if ph.get("mission_vehicle_id") is not None:
            body["mission_vehicle_id"] = vehicle_slug(ph["mission_vehicle_id"])
    # A refusal that never reached Scout stays 409, as this route has always answered: the
    # request was well-formed and the STATE refused it. Once a write was attempted, the status
    # is Scout's own outcome (200 accepted / 400 rejected / 202 unknown / 503 unavailable).
    # Which precondition refused, and whether it was a real mismatch or an unavailable read,
    # is carried by `error_code` and `state` rather than by the status line.
    status = _replan_status_code(post) if post is not None else 409
    return JSONResponse(status_code=status, content=body)


@app.post("/api/vehicles/{vehicle_id}/missions/publish")
async def publish_mission(vehicle_id: str, request: Request):
    """Complete the publication of the vehicle's active planned mission, and report every phase.

    THE ONE authoritative operation. Body: `{ mission_id? }` — optional, and never trusted over
    the durable store: a supplied id must name the vehicle's ACTIVE mission, so a browser
    holding stale state cannot publish an older mission over the operator's latest approval.

    Phases (each reported with its own status and evidence):

        VALIDATING_PLAN               record present, owned by this vehicle, hash intact
        UPLOADING_PIXHAWK             the MISSION_UPLOAD command's verified read-back
        VERIFYING_PIXHAWK             a LIVE, complete read-back: route hash AND route count
        PERSISTING_OPERATOR_MISSION   the store's ACTIVE mission is that same mission
        BUILDING_PLANNING_PACKAGE     replan-planning-package-v1 (fails closed)
        SYNCING_SCOUT_PACKAGE         one POST to Scout's single slot
        VERIFYING_SCOUT_PACKAGE       Scout's read-back proves id == hash == count
        READY                         agent_ready — and not one phase earlier

    RESUMABLE, NOT BLOCKING. The Pixhawk write is the at-least-once MISSION_UPLOAD command
    queue; while that command is in flight this answers 202 with phase UPLOADING_PIXHAWK and
    `state: UPLOAD_IN_PROGRESS`. Call it again when the command verifies. IDEMPOTENT: re-running
    it after READY re-proves everything and answers READY again, creating no new mission id.

    Issues NO vehicle command. If Scout cannot be reached the VERIFIED Pixhawk mission and the
    active record are PRESERVED and the mission is durably marked PACKAGE_SYNC_REQUIRED; the
    retry is POST .../replan/planning-package/sync, which sends only the package."""
    target, err = _local_agent_target(vehicle_id, "replanning")
    if err is not None:
        return err
    vid, base = target
    try:
        body = await request.json()
    except Exception:
        body = {}
    env = mission_publish.run_publish(_publish_deps(), vid, base, vehicle_slug(vid),
                                      mission_id=(body or {}).get("mission_id"))
    post = env.get("scout_post")
    if post is not None:
        _record_replan_operation(vid, post, mission_id=env.get("mission_id"))
    return JSONResponse(status_code=mission_publish.status_code(env), content=env)


@app.get("/api/vehicles/{vehicle_id}/missions/publish")
def publish_mission_state(vehicle_id: str):
    """The vehicle's publication state WITHOUT running anything: the active mission, its upload
    status, whether a package sync is owed, and the last publish attempt. Read-only — it makes
    no Scout call and no Pixhawk download, so Map/Agent can consult it on an ordinary refresh."""
    vid = parse_vehicle_id(vehicle_id)
    if vid not in known_vehicle_ids():
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "unknown vehicle", "vehicle_id": vehicle_id})
    mid = active_original_by_vehicle.get(vid)
    rec = original_missions.get(mid) if mid else None
    last = next((o for o in reversed(publish_operations)
                 if o.get("vehicle_id") == vehicle_slug(vid)), None)
    return {
        "ok": True, "vehicle_id": vehicle_slug(vid), "mission_id": mid,
        "record_present": rec is not None,
        "upload_status": rec.get("upload_status") if rec else None,
        "route_hash": rec.get("route_hash") if rec else None,
        "route_waypoint_count": len(rec.get("route_waypoints") or []) if rec else None,
        "package_sync_state": rec.get("package_sync_state") if rec else None,
        "package_sync_error": rec.get("package_sync_error") if rec else None,
        "package_synced_at": rec.get("package_synced_at") if rec else None,
        "publishing": mission_publish.is_publishing(vid),
        "last_publish": last,
        # The last reconciliation verdict, or an honest "no evidence yet" when this backend has
        # not read the vehicle since it started. Read-only: this route still makes no Scout call
        # and no Pixhawk download — it reports the verdict the readiness path computed.
        "reconciliation": reconciliation_for(vid),
    }


@app.get("/api/diagnostics")
def diagnostics():
    """WHICH BACKEND IS ANSWERING. Process id, when it started, which mission store it is using,
    what it believes each vehicle's active mission is, and the last publish operation.

    This exists because of a real failure mode, not for completeness: on Windows a second
    `run_operator_backend.ps1` fails to bind with WinError 10048 while the FIRST backend keeps
    serving 8210 with its own, older, in-memory active mission — and nothing in the UI could
    tell them apart. Read-only; makes no vehicle or Scout call."""
    return {
        "ok": True,
        "pid": os.getpid(),
        "started_at": PROCESS_STARTED_AT,
        "mission_store_path": str(MISSION_STORE_PATH),
        "mission_store_exists": MISSION_STORE_PATH.exists(),
        "mission_record_count": len(original_missions),
        "active_missions": [
            {
                "vehicle_id": vehicle_slug(v),
                "mission_id": m,
                "upload_status": (original_missions.get(m) or {}).get("upload_status"),
                "route_hash": (original_missions.get(m) or {}).get("route_hash"),
                "package_sync_state": (original_missions.get(m) or {}).get("package_sync_state"),
                "publishing": mission_publish.is_publishing(v),
            }
            for v, m in sorted(active_original_by_vehicle.items())
        ],
        "last_publish": publish_operations[-1] if publish_operations else None,
        "publish_operation_count": len(publish_operations),
    }


@app.get("/api/missions/publish/operations")
def publish_operation_trace(vehicle_id: Optional[str] = None, limit: int = 100):
    """The publish trace, newest last, optionally filtered to one vehicle."""
    items = publish_operations
    if vehicle_id is not None:
        slug = vehicle_slug(parse_vehicle_id(vehicle_id))
        items = [o for o in items if o.get("vehicle_id") == slug]
    if limit and limit > 0:
        items = items[-limit:]
    return {"ok": True, "operations": items, "count": len(publish_operations)}


@app.post("/api/vehicles/{vehicle_id}/replan/planning-package/sync")
async def replan_sync_package(vehicle_id: str, request: Request):
    """MANUAL, explicit synchronization of the approved planning package to Scout.

    This is the ONLY path that sends a package, and it is never called by polling — an
    operator (or an operator-driven test) invokes it deliberately. Body: `{ mission_id? }`,
    defaulting to the vehicle's active original mission.

    It is also the RETRY action the Plan page offers when a mission was verified on the flight
    controller but its package did not reach Scout. That is why it is package-only: this route
    re-verifies and re-sends the PACKAGE, and it provably cannot re-upload the mission, because
    mission_publish.py contains no code that issues a vehicle command.

    It fails closed at the same gates as the full publish, in order, and reports WHICH one
    refused:

      1. the vehicle resolves to a canonical id with a Local Agent (8090) route;
      2. an immutable revision-0 original mission record exists AND belongs to this vehicle,
         and is the vehicle's ACTIVE mission (a stale older mission is never syncable);
      3. its upload_status is VERIFIED (an unverified upload is not an approved mission);
      4. a LIVE Pixhawk read-back is reachable and NOT partial (a truncated download proves
         nothing about what is on the flight controller);
      5. the record's route_hash equals the read-back's route_content_hash, and the route
         waypoint counts agree under the mission-contract Home rule;
      6. the record builds a complete replan-planning-package-v1 (no thinned metadata);
      7. Scout accepts the POST;
      8. Scout's READ-BACK of the stored package carries the same mission id, route hash and
         route waypoint count. Only then is `agent_ready` true.

    A SECOND live read-back runs after the write, so acceptance is bracketed by two consistency
    reads: the route on the flight controller is proven unchanged across it. That second
    download is the cost of a WRITE only — ordinary readiness polling never pays it.

    Issues no vehicle command: it does not arm, change mode, upload, clear or execute
    anything. It writes one package to Scout's single package slot and reads it back."""
    target, err = _replan_target(vehicle_id)
    if err is not None:
        return err
    vid, base = target
    try:
        body = await request.json()
    except Exception:
        body = {}

    # The gates, the package build, the POST and the package read-back all live in
    # mission_publish.run_publish — the SAME transaction the Plan-page publish runs. This route
    # is the PACKAGE-ONLY entry point into it: it re-verifies everything and re-sends the
    # package, and it cannot upload a mission, because that code does not exist in that module.
    env = mission_publish.run_publish(_publish_deps(), vid, base, vehicle_slug(vid),
                                      mission_id=(body or {}).get("mission_id"),
                                      package_only=True)
    post = env.get("scout_post")
    if post is not None:
        _record_replan_operation(vid, post, mission_id=env.get("mission_id"))
    return _sync_response(vid, base, env)


@app.delete("/api/vehicles/{vehicle_id}/replan/planning-package")
def replan_delete_package(vehicle_id: str):
    """Clear Scout's stored planning package. Idempotent — safe when nothing is stored."""
    target, err = _replan_target(vehicle_id)
    if err is not None:
        return err
    vid, base = target
    result = scout_replan.delete_planning_package(base)
    _record_replan_operation(vid, result)
    return _replan_response(vid, result)


@app.get("/api/vehicles/{vehicle_id}/replan/experiment")
def replan_get_experiment(vehicle_id: str):
    """Scout's accepted energy-replanning injection state (always SIMULATED, auto-expiring)."""
    target, err = _replan_target(vehicle_id)
    if err is not None:
        return err
    vid, base = target
    return _replan_response(vid, scout_replan.get_experiment(base))


@app.put("/api/vehicles/{vehicle_id}/replan/experiment")
async def replan_put_experiment(vehicle_id: str, request: Request):
    """Apply ONE explicit energy-replanning injection (force_safe_return / energy_margin_percent
    / battery_percent / duration_s). target_vehicle is forced to the path vehicle — an injection
    can only ever target the selected Scout. At least one override is required. Recorded as a
    write; the frontend must never resend this during polling."""
    target, err = _replan_target(vehicle_id)
    if err is not None:
        return err
    vid, base = target
    try:
        body = await request.json()
    except Exception:
        body = {}
    overrides = {k: (body or {}).get(k) for k in REPLAN_EXPERIMENT_FIELDS
                 if (body or {}).get(k) is not None}
    if not overrides:
        return JSONResponse(status_code=400, content={
            "ok": False, "vehicle_id": vehicle_slug(vid),
            "error": "at least one override is required",
            "fields": sorted(REPLAN_EXPERIMENT_FIELDS)})
    overrides["target_vehicle"] = vehicle_slug(vid)   # isolation: never another vehicle
    result = scout_replan.put_experiment(base, overrides)
    _record_replan_operation(vid, result)
    return _replan_response(vid, result)


@app.delete("/api/vehicles/{vehicle_id}/replan/experiment")
def replan_delete_experiment(vehicle_id: str):
    """Clear Scout's energy-replanning injection. Idempotent — safe when nothing is active."""
    target, err = _replan_target(vehicle_id)
    if err is not None:
        return err
    vid, base = target
    result = scout_replan.delete_experiment(base)
    _record_replan_operation(vid, result)
    return _replan_response(vid, result)


@app.post("/api/vehicles/{vehicle_id}/replan/reset")
def replan_reset(vehicle_id: str):
    """Rearm Scout's replanning controller from a terminal state (rearms transaction state,
    clears the energy debounce). Issues NO vehicle command, does NOT change vehicle mode, does
    NOT clear/restore any Pixhawk mission. Rejected (409) while a transaction is active."""
    target, err = _replan_target(vehicle_id)
    if err is not None:
        return err
    vid, base = target
    result = scout_replan.post_reset(base)
    _record_replan_operation(vid, result)
    return _replan_response(vid, result)


@app.get("/api/vehicles/{vehicle_id}/replan/readiness")
def replan_readiness(vehicle_id: str):
    """The combined MISSION READY / REPLANNING READY summary (task Section 3). Distinguishes the
    Vehicle mission (Pixhawk upload + verified readback) from the Scout planning package (stored,
    consistent, geometry). Never hides a package failure behind a successful Pixhawk upload, and
    lists limitations (absent boundary, connector not proven safe, shoreline metadata only, hash
    comparison unavailable) separately. Read-only."""
    target, err = _replan_target(vehicle_id)
    if err is not None:
        return err
    vid, base = target
    return _compute_replan_readiness(vid, base)


@app.get("/api/replan/operations")
def replan_operation_trace(vehicle_id: Optional[str] = None, limit: int = 200):
    """The write operation trace (planning-package / config / experiment / reset), newest last.
    Optionally filtered to one vehicle. This is the reconnect-safe record of accepted / rejected
    / unknown supervisory writes — the frontend renders it rather than re-deriving events."""
    items = replan_operations
    if vehicle_id is not None:
        vid = parse_vehicle_id(vehicle_id)
        items = [o for o in items if o["vehicle_id"] == vehicle_slug(vid)]
    if limit and limit > 0:
        items = items[-limit:]
    return {"ok": True, "operations": items, "count": len(replan_operations),
            "generated_at": datetime.now(timezone.utc).isoformat()}


# ── Mission-execution lifecycle (Scout Local Agent /agent/mission_execution/*, port 8090) ──
# Scout owns this lifecycle OUTRIGHT — Start is ONE Scout-side transaction (verified LOITER →
# set Home to the launch position → verify Home → synchronize the planning package → verified
# AUTO → progression confirmation → RUNNING), and Pause/Resume are likewise single transactions.
# The Operator Station therefore recreates NO mission-execution FSM, issues NO separate LOITER /
# SET_HOME / AUTO commands for Start, and routes NONE of this through the operator command queue
# or the Flask (8080) Pixhawk surface. It forwards an explicit operator intent, preserves Scout's
# body, records the write, and reconciles an operation whose HTTP verdict was lost.
#
# A write is NEVER auto-resent: an UNKNOWN outcome (timeout / ambiguous 5xx) is resolved by
# READING canonical status (scout_mission_execution.reconcile) — resending a Start could re-run a
# whole Home/AUTO transaction the vehicle already performed.
MAX_MISSION_EXECUTION_OPERATIONS = 2000
mission_execution_operations = []   # [ {seq, vehicle_id, operation, requested_at, outcome, ...} ]
_mx_op_seq = 0

# Per-vehicle observation memory, used ONLY to deduplicate lifecycle events sourced from status
# POLLING. Keyed by canonical vehicle id, so one vehicle's lifecycle can never suppress or emit
# another's. Reads never write to Scout — this is operator-side memory of what was already logged.
_mx_observed = {}   # vid -> {history:set(), state:(state,effective), arrival:bool, final_loiter:bool}


def _mx_memory(vid):
    return _mx_observed.setdefault(vid, {
        "history": set(), "state": None, "arrival": False, "final_loiter": False})


def _mx_event(vid, *, severity, message, detail):
    _append_event(severity=severity, etype="mission-execution", source="operator-backend",
                  vehicle_id=vid, vehicle=name_of(vid), message=message, detail=detail)


def _record_mission_execution_operation(vid, result, *, requested_at, mission_id=None,
                                        reconciliation=None):
    """Append ONE mission-execution write to the operation trace and the operator event log.

    Records everything the task requires to audit a run without re-deriving it: vehicle id,
    operation, requested timestamp, operational outcome (accepted / failed / rejected / unknown
    / unavailable / unsupported), Scout's HTTP status, Scout's error code, Scout's operation id,
    the mission id, the resulting lifecycle state, and the reconciliation verdict when the
    outcome was unknown. Reads are NOT logged — only writes, which are the operations a
    reconnect must never duplicate."""
    global _mx_op_seq
    _mx_op_seq += 1
    seq = result.get("sequence") or {}
    entry = {
        "seq": _mx_op_seq,
        "vehicle_id": vehicle_slug(vid),
        "vehicle": name_of(vid),
        "operation": (result.get("operation") or "").replace("mission_execution.", ""),
        "requested_at": requested_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "outcome": result.get("operational_outcome"),
        "transport_outcome": result.get("outcome"),
        "http_status": result.get("http_status"),
        "scout_error_code": result.get("scout_error_code"),
        "scout_error_message": result.get("scout_error_message"),
        "operation_id": result.get("operation_id"),
        "mission_id": result.get("mission_id") or mission_id,
        "route_hash": result.get("route_hash"),
        "previous_state": result.get("previous_state"),
        "resulting_state": result.get("current_state"),
        "verified_mode": result.get("verified_mode"),
        "final": result.get("final"),
        "idempotent": result.get("idempotent"),
        "home_result": result.get("home_result"),
        "sequence": result.get("sequence"),
        "continuation_verified": seq.get("continuation_verified"),
        "supported": result.get("supported", True),
        "unknown": result.get("operational_outcome") == scout_mission_execution.OUTCOME_UNKNOWN,
        "reconciliation": reconciliation,
    }
    mission_execution_operations.append(entry)
    if len(mission_execution_operations) > MAX_MISSION_EXECUTION_OPERATIONS:
        del mission_execution_operations[
            :len(mission_execution_operations) - MAX_MISSION_EXECUTION_OPERATIONS]

    outcome = entry["outcome"]
    sev = ("warning" if outcome in (scout_mission_execution.OUTCOME_UNKNOWN,
                                    scout_mission_execution.OUTCOME_FAILED)
           else "caution" if outcome == scout_mission_execution.OUTCOME_REJECTED else "info")
    msg = f"Mission {entry['operation']} -> {outcome}"
    if entry["scout_error_code"]:
        msg += f" ({entry['scout_error_code']})"
    elif entry["resulting_state"]:
        msg += f" ({entry['resulting_state']})"
    _mx_event(vid, severity=sev, message=msg, detail=entry)

    # The continuation warning is its own event: Scout can report RUNNING and verified AUTO while
    # continuation_verified is false, and that must never be rounded up to "resumed successfully".
    if entry["operation"] == "resume" and seq.get("continuation_verified") is False:
        _mx_event(vid, severity="warning", detail=entry,
                  message="AUTO resumed, but waypoint continuation was NOT verified - the "
                          "Pixhawk may have restarted the mission at waypoint 0")
    return entry


def _record_lifecycle_transaction(vid, env, *, requested_at):
    """Append ONE orchestrated lifecycle transaction (start / pause / resume / stop) to the same
    write trace the raw operations use, so the Agent page's history renders both without
    branching. Adds what only a transaction has: its phases and the authority hand-off it
    performed, which is the audit trail for "who held the wheel, and who moved it"."""
    global _mx_op_seq
    _mx_op_seq += 1
    seq = env.get("sequence") if isinstance(env.get("sequence"), dict) else {}
    entry = {
        "seq": _mx_op_seq,
        "vehicle_id": vehicle_slug(vid),
        "vehicle": name_of(vid),
        "operation": env.get("operation"),
        "requested_at": requested_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "outcome": env.get("outcome"),
        "transport_outcome": env.get("outcome"),
        "http_status": env.get("http_status"),
        "scout_error_code": env.get("scout_error_code"),
        "scout_error_message": env.get("scout_error_message"),
        "operation_id": env.get("operation_id"),
        "mission_id": env.get("mission_id"),
        "route_hash": env.get("route_hash"),
        "previous_state": env.get("previous_state"),
        "resulting_state": env.get("resulting_state"),
        "verified_mode": env.get("verified_mode"),
        "final": env.get("final"),
        "idempotent": env.get("idempotent"),
        "home_result": env.get("home_result"),
        "sequence": env.get("sequence"),
        # Scout's Stop evidence (hold verified, original restored, hashes, rewind verified,
        # sequence after, replan/experiment reset, authority after, outcome), preserved on the
        # trace entry so the Agent page's history renders the real reset rather than re-deriving
        # one. Null unless Scout actually REPORTED a stop block — an unreported one is recorded
        # as absent rather than as a row of nulls that reads like evidence.
        "stop": (env["stop"] if isinstance(env.get("stop"), dict) and env["stop"].get("reported")
                 else None),
        "continuation_verified": seq.get("continuation_verified"),
        "supported": env.get("supported", True),
        "unknown": env.get("outcome") == scout_mission_execution.OUTCOME_UNKNOWN,
        "reconciliation": env.get("reconciliation"),
        "error": env.get("error"),
        # What only a TRANSACTION has: the phases it ran and the authority hand-off it
        # performed. This is the audit trail for "who held the wheel, and who moved it".
        "phases": env.get("phases"),
        "authority": env.get("authority") or {},
    }
    mission_execution_operations.append(entry)
    if len(mission_execution_operations) > MAX_MISSION_EXECUTION_OPERATIONS:
        del mission_execution_operations[
            :len(mission_execution_operations) - MAX_MISSION_EXECUTION_OPERATIONS]

    outcome = entry["outcome"]
    sev = ("warning" if outcome in (scout_mission_execution.OUTCOME_UNKNOWN,
                                    scout_mission_execution.OUTCOME_FAILED)
           else "caution" if outcome in (scout_mission_execution.OUTCOME_REJECTED,
                                         mission_lifecycle.OUTCOME_BLOCKED)
           else "info")
    msg = f"Mission {entry['operation']} -> {outcome}"
    if entry["scout_error_code"]:
        msg += f" ({entry['scout_error_code']})"
    elif outcome == mission_lifecycle.OUTCOME_BLOCKED and env.get("error_code"):
        msg += f" ({env['error_code']})"
    elif entry["resulting_state"]:
        msg += f" ({entry['resulting_state']})"
    _mx_event(vid, severity=sev, message=msg, detail=entry)

    if entry["operation"] == "resume" and seq.get("continuation_verified") is False:
        _mx_event(vid, severity="warning", detail=entry,
                  message="AUTO resumed, but waypoint continuation was NOT verified - the "
                          "Pixhawk may have restarted the mission at waypoint 0")
    return entry


def _mx_ingest_status_events(vid, summary, body):
    """Emit lifecycle events observed through status POLLING, deduplicated per vehicle.

    Two sources, never both for the same fact: Scout's own transition `history` when it supplies
    one (each entry logged once, by fingerprint), otherwise the operator's observation of a
    CHANGED (state, effective_state) pair. Return-completion milestones (arrival confirmed, final
    LOITER verified) are latched so a repeating poll cannot re-log them."""
    if not summary.get("present"):
        return
    mem = _mx_memory(vid)

    history = body.get("history") if isinstance(body.get("history"), list) else []
    logged_history = False
    for item in history[-50:]:
        if not isinstance(item, dict):
            continue
        fp = json.dumps({k: item.get(k) for k in
                         ("timestamp", "ts", "at", "from", "to", "state", "operation_id",
                          "reason", "event")}, sort_keys=True, default=str)
        if fp in mem["history"]:
            continue
        mem["history"].add(fp)
        logged_history = True
        frm = item.get("from") or item.get("from_state")
        to = item.get("to") or item.get("to_state") or item.get("state")
        reason = item.get("reason") or item.get("event")
        # ASCII arrows only: these messages are also printed to the operator console, which on
        # Windows is cp1252 and cannot encode "→".
        _mx_event(vid, severity="info", detail={"source": "scout-history", **item},
                  message=f"Mission execution {frm or '?'} -> {to or '?'}"
                          + (f" ({reason})" if reason else ""))
    if len(mem["history"]) > 500:
        mem["history"] = set(list(mem["history"])[-500:])

    observed = (summary.get("state"), summary.get("effective_state"))
    if not logged_history and mem["state"] != observed:
        if mem["state"] is not None:      # first observation is a baseline, not a transition
            _mx_event(vid, severity="info",
                      detail={"source": "operator-observed", "state": observed[0],
                              "effective_state": observed[1], "mode": summary.get("mode"),
                              "mission_id": summary.get("mission_id"),
                              "active_operation_id": summary.get("active_operation_id")},
                      message=f"Mission execution state {mem['state'][0] or '?'} -> "
                              f"{observed[0] or '?'}"
                              + (f" (effective {observed[1]})"
                                 if observed[1] and observed[1] != observed[0] else ""))
        mem["state"] = observed

    rc = summary.get("return_completion") or {}
    if rc.get("arrival_confirmed") is True and not mem["arrival"]:
        mem["arrival"] = True
        _mx_event(vid, severity="info", detail={"source": "return-completion", **rc},
                  message="Home arrival confirmed by Scout")
    if rc.get("final_loiter_verified") is True and not mem["final_loiter"]:
        mem["final_loiter"] = True
        _mx_event(vid, severity="info", detail={"source": "return-completion", **rc},
                  message="Final LOITER verified - mission execution complete (COMPLETED_HOLD)")
    # A rearm/new run clears the latches so the NEXT run can log its own milestones.
    if summary.get("state") in ("READY", "NOT_READY"):
        mem["arrival"] = mem["final_loiter"] = False


def _mx_status_code(result):
    """The honest HTTP status for a mission-execution outcome. Deliberately non-500 for every
    handled case so the frontend poll never sees a console error for an unreachable/older Scout,
    and deliberately 200 for a vehicle-level FAILURE — Scout processed the request, the vehicle
    operation did not succeed, and the body (ok:false + Scout's error code) says exactly that."""
    outcome = result.get("operational_outcome", result.get("outcome"))
    if outcome == scout_mission_execution.OUTCOME_UNSUPPORTED:
        return 200          # older Scout — a handled, honest "not supported", not an error
    if outcome == scout_mission_execution.OUTCOME_UNAVAILABLE:
        return 503          # a read that failed — honest "unavailable", never fabricated state
    if outcome == scout_mission_execution.OUTCOME_UNKNOWN:
        return 202          # accepted-but-unconfirmed: reconciled by a read, never resent
    if outcome == scout_mission_execution.OUTCOME_REJECTED:
        # Preserve Scout's own 409 (precondition / lifecycle / replanning / arbitration).
        return 409 if result.get("http_status") == 409 else 400
    return 200


def _mx_response(vid, result):
    result = dict(result)
    result["vehicle_id"] = vehicle_slug(vid)
    return JSONResponse(status_code=_mx_status_code(result), content=result)


def _mission_execution_write(vehicle_id, operation, fn, *, mission_id=None):
    """Shared body for the four write routes: resolve the SELECTED vehicle's Local Agent, issue
    the operation, interpret Scout's body (a 200 carrying `error` is a FAILURE, not a success),
    reconcile an UNKNOWN outcome with a status read, and record the write."""
    target, err = _local_agent_target(vehicle_id, "mission-execution")
    if err is not None:
        return err
    vid, base = target
    requested_at = datetime.now(timezone.utc).isoformat()
    result = scout_mission_execution.interpret_operation(fn(base))

    reconciliation = None
    if result.get("operational_outcome") == scout_mission_execution.OUTCOME_UNKNOWN:
        # No verdict reached us. Read Scout's canonical status and resolve from ITS state —
        # never a blind resend of a transaction that may already have run on the vehicle.
        reconciliation = scout_mission_execution.reconcile(
            base, operation, expected_mission_id=mission_id)
        result["reconciliation"] = reconciliation
    result["requested_mission_id"] = mission_id
    _record_mission_execution_operation(vid, result, requested_at=requested_at,
                                        mission_id=mission_id, reconciliation=reconciliation)
    return _mx_response(vid, result)


@app.get("/api/vehicles/{vehicle_id}/mission-execution/status")
def mission_execution_status(vehicle_id: str):
    """Scout's canonical mission-execution status, pulled live from the SELECTED vehicle's Local
    Agent. Read-only. The response carries Scout's body verbatim under `scout` plus a derived
    `summary` — every field of which is Scout's word or None. An older Scout that 404s the route
    is supported:false; READY, can_start, verified Home, continuation and completion are NEVER
    fabricated. Polling this route also feeds the deduplicated lifecycle event log."""
    target, err = _local_agent_target(vehicle_id, "mission-execution")
    if err is not None:
        return err
    vid, base = target
    result = scout_mission_execution.get_status(base)
    summary = scout_mission_execution.summarize_status(result)
    body = result.get("scout") if isinstance(result.get("scout"), dict) else {}
    _mx_ingest_status_events(vid, summary, body)
    out = dict(result)
    out["summary"] = summary
    return _mx_response(vid, out)


# ── The ONE endpoint per operator intent (authority orchestration included) ────────────────
# The frontend calls exactly one of these per button press. The authority hand-off is NOT a
# separate call the browser makes first — mission_lifecycle.py performs it, verifies it by
# read-back, and reports it as a PHASE of the same operation. That is the whole point: the
# operator never has to press Release Control, and the two halves can never be issued out of
# order or half-done by a page that forgot one.
def _lifecycle_deps():
    """The operator-backend facts the orchestration layer runs on. Built per request so a test
    that swaps a store or a transport sees the swap."""
    return mission_lifecycle.Deps(
        active_mission_id=lambda vid: active_original_by_vehicle.get(vid),
        mission_record=lambda mid: original_missions.get(mid),
        # `fresh=True` is passed by the START transaction only, and it forces a live Pixhawk
        # mission download instead of the bounded polling cache. That is what makes the Start
        # proof a proof about NOW — and it is why the station no longer has to poll a copy of it.
        readiness=lambda vid, base, fresh=False: _compute_replan_readiness(
            vid, base, max_readback_age_s=0.0 if fresh else PIXHAWK_READBACK_TTL_S),
        get_authority=read_control_authority,
        set_authority=lambda vid, value: apply_control_authority(
            vid, value, source="mission-execution")[0],
    )


def _lifecycle_transaction(vehicle_id, operation, runner):
    """Shared body for start / pause / resume / stop: resolve the vehicle's Local Agent, run the
    ONE transaction, record it in the write trace and answer at its honest HTTP status."""
    target, err = _local_agent_target(vehicle_id, "mission-execution")
    if err is not None:
        return err
    vid, base = target
    requested_at = datetime.now(timezone.utc).isoformat()
    env = runner(_lifecycle_deps(), vid, base, vehicle_slug(vid))
    _record_lifecycle_transaction(vid, env, requested_at=requested_at)
    return JSONResponse(status_code=mission_lifecycle.status_code(env), content=env)


@app.post("/api/vehicles/{vehicle_id}/mission-execution/start")
async def mission_execution_start(vehicle_id: str, request: Request):
    """START — one operation, two phases: verified authority transfer, then Scout's own Start.

    The Operator issues NO separate LOITER, Set Home or AUTO command; Scout performs and
    verifies each step. Before anything is sent it requires the vehicle's ACTIVE PERSISTED
    mission record to be VERIFIED, the Pixhawk read-back hash to match, the planning package to
    be stored/usable/consistent, Scout's replanning readiness to be true, and Scout's own Start
    eligibility. It then transfers authority to LOCAL_AGENT and READS IT BACK before contacting
    Scout at all.

    Body: { mission_id? } — OPTIONAL and never trusted over the persisted record. The active
    mission id is what is forwarded; a supplied id that does not match it is rejected here, so
    a browser can never point a Start at a route the operator did not approve.

    Start RESETS Home to the vehicle's current launch position (Scout sets and verifies it); the
    originally planned Home is not retained."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    supplied = (body or {}).get("mission_id")
    return _lifecycle_transaction(
        vehicle_id, "start",
        lambda deps, vid, base, slug: mission_lifecycle.run_start(
            deps, vid, base, slug, supplied_mission_id=supplied))


@app.post("/api/vehicles/{vehicle_id}/mission-execution/pause")
def mission_execution_pause(vehicle_id: str):
    """PAUSE — authority stays LOCAL_AGENT (the mission is still the agent's to run). Scout
    records the mission sequence, commands a VERIFIED LOITER and confirms the mission is still
    loaded; the operator backend then verifies PAUSED/LOITER against canonical status. This is
    NOT a stop/cancel — it clears no mission, uploads no replacement and resets no sequence."""
    return _lifecycle_transaction(
        vehicle_id, "pause",
        lambda deps, vid, base, slug: mission_lifecycle.run_pause(deps, vid, base, slug))


@app.post("/api/vehicles/{vehicle_id}/mission-execution/resume")
def mission_execution_resume(vehicle_id: str):
    """RESUME — verifies authority is STILL LOCAL_AGENT and re-acquires it only if it is not,
    then Scout verifies the expected mission is loaded, verifies Home and position, commands a
    VERIFIED AUTO and observes the sequence. Scout can report RUNNING with
    continuation_verified=false — the mode transition worked but continuation from the paused
    waypoint could not be proven. That is preserved and surfaced as a warning, never as
    success."""
    return _lifecycle_transaction(
        vehicle_id, "resume",
        lambda deps, vid, base, slug: mission_lifecycle.run_resume(deps, vid, base, slug))


@app.post("/api/vehicles/{vehicle_id}/mission-execution/stop")
def mission_execution_stop(vehicle_id: str):
    """STOP — a SAFE ABORT of the mission run, proxied to POST /agent/mission_execution/stop.

    This is NOT the legacy raw Pixhawk stop, and the Operator reimplements NONE of the sequence.
    Scout performs the whole transaction: verified LOITER, verify the active mission identity,
    restore the immutable original mission if a verified revised route is installed, rewind the
    original to its start, verify the rewind, reset mission-execution / replan / test state,
    clear the simulated experiment injection, invalidate the prior runtime Home, return
    supervisory authority to OPERATOR, and re-prove the mission evidence.

    The Operator forwards the ACTIVE PERSISTED mission id (so Scout can fail closed on a
    mismatch), re-reads canonical status and preserves Scout's `stop` evidence verbatim. It
    issues no LOITER, no upload, no rewind, no reset, no rearm and no authority write.

    A successful Stop normally rests at state=NOT_READY with start_eligible=true and
    authority_blocks_start=true. That is EXPECTED — authority is deliberately back with the
    OPERATOR — and is never reported as a mission failure; the Start transaction performs the
    hand-off back to LOCAL_AGENT. A Stop that fails after the safe hold leaves Scout SUSPENDED
    with its own code (STOP_ACTIVE_MISSION_UNKNOWN / STOP_RESTORE_UPLOAD_FAILED /
    STOP_RESTORE_HASH_MISMATCH / STOP_REWIND_NOT_VERIFIED), which is surfaced verbatim; the
    backend never follows it with an automatic Rearm, Resume, AUTO or second Stop."""
    return _lifecycle_transaction(
        vehicle_id, "stop",
        lambda deps, vid, base, slug: mission_lifecycle.run_stop(deps, vid, base, slug))


@app.get("/api/vehicles/{vehicle_id}/mission-execution/preflight")
def mission_execution_preflight(vehicle_id: str):
    """READ-ONLY Start preflight: the resolved active mission identity plus the five precondition
    checks, computed by the SAME function the Start transaction enforces — so the Map card's
    readiness display and the gate can never disagree. Issues no write of any kind."""
    target, err = _local_agent_target(vehicle_id, "mission-execution")
    if err is not None:
        return err
    vid, base = target
    out = mission_lifecycle.preflight(_lifecycle_deps(), vid, base)
    out["vehicle_id"] = vehicle_slug(vid)
    return out


@app.post("/api/vehicles/{vehicle_id}/mission-execution/rearm")
def mission_execution_rearm(vehicle_id: str):
    """Rearm the Local Agent's mission-execution controller from a terminal state (COMPLETED_HOLD
    / SUSPENDED / FAILED). Issues NO vehicle command, does NOT change vehicle mode, does NOT clear
    the Pixhawk mission and does NOT re-upload the original mission — it only prepares the
    controller for another explicitly prepared run. This is not a vehicle reset."""
    return _mission_execution_write(vehicle_id, "rearm", scout_mission_execution.post_rearm)


# ── FULL REFRESH — one bounded, single-flight, READ-ONLY operation that reconstructs the
# entire current mission/readiness evidence graph on demand, without uploading a mission. See
# mission_full_refresh.py for the full rationale; this is the thin FastAPI adapter over it,
# following the same shape as _lifecycle_deps/_lifecycle_transaction above. ────────────────────
MAX_FULL_REFRESH_OPERATIONS = 200
full_refresh_operations = []   # [ {seq, vehicle_id, operation_id, ok, reconciliation, ...} ]
_full_refresh_seq = 0


def _record_full_refresh_operation(result):
    """Append ONE Full Refresh transaction to its own trace (diagnostics only — this is a read,
    never a vehicle write, so it is deliberately NOT folded into mission_execution_operations,
    whose whole point is auditing writes). One concise log line per explicit operation (task
    Section 29) — never per ordinary poll, because Full Refresh is never called by one."""
    global _full_refresh_seq
    _full_refresh_seq += 1
    mission = result.get("mission") or {}
    binding = result.get("binding") or {}
    readiness = result.get("readiness") or {}
    entry = {
        "seq": _full_refresh_seq, "vehicle_id": result.get("vehicle_id"),
        "operation_id": result.get("operation_id"), "ok": result.get("ok"),
        "started_at": result.get("started_at"), "completed_at": result.get("completed_at"),
        "duration_s": result.get("duration_s"),
        "reconciliation": mission.get("reconciliation"),
        # `binding_state` is diagnostic only — UNBOUND is the expected, healthy value for an idle
        # mission (see mission_full_refresh.py), never treated as this operation's verdict.
        "binding_state": binding.get("binding_state"),
        "reproof_outcome": binding.get("reproof_outcome"),
        "reproof_supported": binding.get("reproof_supported"),
        "can_start": readiness.get("can_start"),
        "error_code": result.get("error_code"),
    }
    full_refresh_operations.append(entry)
    if len(full_refresh_operations) > MAX_FULL_REFRESH_OPERATIONS:
        del full_refresh_operations[:len(full_refresh_operations) - MAX_FULL_REFRESH_OPERATIONS]
    print(f"[FULL_REFRESH] {entry['vehicle_id']} op={entry['operation_id']} "
          f"reconciliation={entry['reconciliation']} binding={entry['binding_state']} "
          f"reprove={entry['reproof_outcome']} can_start={entry['can_start']} ok={entry['ok']} "
          f"duration={entry['duration_s']}s")
    return entry


def _home_view_for_full_refresh(vid):
    """The current, read-only Home view for one vehicle (task Section 13) — Scout's own
    home_status, mirrored via the SAME home_block() every fleet row already uses, fed from this
    vehicle's own last raw packet only (never another vehicle's, never a blank template unless it
    has genuinely never reported). No outbound request of any kind: Scout already pushes
    home_status on every status packet, and reading it back out is the existing read-only Home
    recovery path — there is no separate GET Home route to call, and none is added here."""
    rec = current_vehicle_state.get(vid)
    raw = rec.get("raw_latest") if isinstance(rec, dict) else None
    return home_block(vid, raw or {}, {})


def _agent_state_for_full_refresh(vid, flask_base):
    """Best-effort, read-only GET of Scout's /agent/state (task Section 14). Never raises and
    never gates anything downstream: an unreachable or unsupported Scout is reported exactly that
    way, like every other proxy in this file, and Full Refresh proceeds without it."""
    if not flask_base:
        return None
    try:
        r = requests.get(f"{flask_base}/agent/state", timeout=8)
    except requests.RequestException as exc:
        return {"reachable": False, "supported": True, "error": str(exc)}
    if r.status_code == 404:
        return {"reachable": True, "supported": False,
                "error": "This Scout does not implement /agent/state"}
    try:
        body = r.json() if r.content else {}
    except Exception:
        body = {}
    return {"reachable": True, "supported": True,
            "state": body if isinstance(body, dict) else {}}


def _full_refresh_deps():
    """The operator-backend facts mission_full_refresh runs on. Built per request so a test that
    swaps a store or a transport sees the swap — same idiom as _lifecycle_deps/_publish_deps."""
    return mission_full_refresh.Deps(
        active_mission_id=lambda vid: active_original_by_vehicle.get(vid),
        mission_record=lambda mid: original_missions.get(mid),
        run_preflight=lambda vid, base, *, fresh: mission_lifecycle.preflight(
            _lifecycle_deps(), vid, base, fresh=fresh),
        reprove=scout_mission_execution.post_reprove_binding,
        replan_status=scout_replan.get_status,
        home_view=_home_view_for_full_refresh,
        agent_state=_agent_state_for_full_refresh,
        record_operation=_record_full_refresh_operation,
    )


@app.post("/api/vehicles/{vehicle_id}/mission-execution/full-refresh")
def mission_execution_full_refresh(vehicle_id: str):
    """FULL REFRESH — one bounded, single-flight, READ-ONLY operation that reconstructs the
    entire current mission/readiness evidence graph: the approved mission, a fresh Pixhawk route
    proof, Scout's planning package, three-way reconciliation, a read-only Scout binding-reproof
    attempt, Home, Scout's /agent/state evidence, energy feasibility and risk — returned as ONE
    coherent snapshot generated from evidence gathered in this one operation (mission_full_refresh
    .run_full_refresh).

    Upgrades the EXISTING Agent Mission Refresh button; it is not a second Refresh. Unlike the
    plain preflight GET (fresh=False, may read Pixhawk evidence through a bounded cache), this
    forces the SAME live-evidence proof the Start transaction performs, and additionally asks
    Scout to re-prove mission-execution binding — read-only, no vehicle command of any kind. A
    vehicle that already carries the exact approved mission can recover from UNBOUND /
    MISSION_ROUTE_UNVERIFIED / ROUTE_HASH_STALE WITHOUT a mission re-upload; a genuine mismatch is
    reported honestly (PIXHAWK_MISMATCH / PACKAGE_SYNC_REQUIRED) and never silently repaired.

    POST because it starts a bounded operation (a fresh Pixhawk download, a speculative Scout
    reprove POST), even though nothing about it writes vehicle state. Single-flight per vehicle: a
    second concurrent call for the SAME vehicle is rejected with 409 rather than issuing a second,
    overlapping set of live reads."""
    target, err = _local_agent_target(vehicle_id, "mission-execution")
    if err is not None:
        return err
    vid, base = target
    flask_base = vehicle_api_base(vid)
    try:
        with mission_full_refresh.vehicle_refresh_lock(vid):
            result = mission_full_refresh.run_full_refresh(
                _full_refresh_deps(), vid, base, flask_base, vehicle_slug(vid))
    except mission_full_refresh.Busy as exc:
        return JSONResponse(status_code=409, content={
            "ok": False, "vehicle_id": vehicle_slug(vid), "error": str(exc),
            "error_code": "FULL_REFRESH_BUSY"})
    return result


@app.get("/api/mission-execution/full-refresh/operations")
def mission_execution_full_refresh_trace(vehicle_id: Optional[str] = None, limit: int = 200):
    """The Full Refresh diagnostics trace, newest last, optionally filtered to one vehicle."""
    items = full_refresh_operations
    if vehicle_id is not None:
        vid = parse_vehicle_id(vehicle_id)
        items = [o for o in items if o["vehicle_id"] == vehicle_slug(vid)]
    if limit and limit > 0:
        items = items[-limit:]
    return {"ok": True, "operations": items, "count": len(full_refresh_operations),
            "generated_at": datetime.now(timezone.utc).isoformat()}


@app.get("/api/mission-execution/operations")
def mission_execution_operation_trace(vehicle_id: Optional[str] = None, limit: int = 200):
    """The mission-execution write trace (start / pause / resume / rearm), newest last, optionally
    filtered to one vehicle. This is the reconnect-safe record of accepted / failed / rejected /
    unknown operations — including each unknown's reconciliation verdict — so the frontend renders
    what actually happened instead of re-deriving it from polling."""
    items = mission_execution_operations
    if vehicle_id is not None:
        vid = parse_vehicle_id(vehicle_id)
        items = [o for o in items if o["vehicle_id"] == vehicle_slug(vid)]
    if limit and limit > 0:
        items = items[-limit:]
    return {"ok": True, "operations": items, "count": len(mission_execution_operations),
            "generated_at": datetime.now(timezone.utc).isoformat()}


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse(url="/app/", status_code=307)


class RevalidatingStaticFiles(StaticFiles):
    """StaticFiles that forces the browser to revalidate every asset.

    The operator station is a no-build ES-module app: index.html references app.js, which
    imports pages/*.js, components/*.js and lib/*.js by plain relative path, and there is
    no bundler and therefore no content hash anywhere in a URL. Plain StaticFiles sends
    only `etag` + `last-modified` and NO `Cache-Control`, so a browser falls back to
    heuristic freshness (roughly 10% of the file's age) and may serve some modules from
    cache without revalidating while fetching others.

    That mixes versions of a single deploy, and mixed versions are not a cosmetic problem
    here: an older cached Map.js against a newer theme.css rendered the Leaflet map over
    the entire application shell. `no-cache` does not mean "do not store" — the copy is
    still cached and the etag still yields a cheap 304 — it means "never reuse without
    asking", which is exactly the guarantee an unhashed module graph needs.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "no-cache, must-revalidate")
        return response


# Operator station (design-system frontend) — the only supported dashboard.
# The classic static/ frontend has been retired; "/" redirects here.
app.mount("/app", RevalidatingStaticFiles(directory=BASE_DIR / "operator", html=True), name="operator")