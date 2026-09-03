"""
The thesis Experiment Recorder -- a general-purpose, OBSERVATIONAL evidence
recorder for every experiment in the thesis set. Generic by design: it never
hardcodes an experiment_id/type, so it works unchanged for the final field
families (E1 nominal, E2 energy-triggered adaptation, E3 communication
degradation/loss, E4 operator authority takeover -- energy characterization
is supporting calibration for E2, not a separate family) as well as any
future one.

NON-NEGOTIABLE CONTRACT (task section 0): this module is asynchronous,
best-effort, non-blocking, and FAIL-OPEN with respect to *recording* --
never with respect to vehicle safety. A recorder failure degrades recording;
it must never delay, alter, or block mission execution, replanning, energy
policy, communication handling, or any HTTP endpoint.

Architecture
------------
    caller thread (mission-exec / replan / main loop / HTTP handler)
        record_event() / record_decision() / record_telemetry() / ...
            -- build a small dict, queue.Queue.put_nowait(), return --
                        │
                bounded queue (high-priority / low-priority)
                        │
                dedicated background writer thread (the ONLY thread that
                touches disk for experiment evidence)
                        │
                     disk (experiment_runs/run-.../*.json, *.jsonl, *.csv)

Every public method on ExperimentRecorder is wrapped so it can never raise
into the caller and never blocks the caller: queue writes use put_nowait()
(never put()); a full queue drops the record and increments a counter rather
than waiting; any unexpected exception is swallowed and turns the recorder
DEGRADED rather than propagating.

This module deliberately holds NO opinion about mission/replan/energy/
communication/Home/hashing semantics -- it only serializes what those
modules already decided, from the same authoritative transition points they
already use (transition_log.py, the shared event_callback, _finalize_revision,
the Start binding point) -- see the call sites in local_agent.py,
mission_execution_controller.py, replan_controller.py, transition_log.py.
"""
import csv
import hashlib
import json
import os
import queue
import subprocess
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

import experiment_record_config

SCHEMA_VERSION = "experiment-record-v1"
EVENT_SCHEMA_VERSION = "experiment-event-v1"
SOURCE_LABEL = "SCOUT_LOCAL_AGENT"

# ── Recorder health/lifecycle state (task section 33) ────────────────────────
STATE_DISABLED = "DISABLED"
STATE_HEALTHY = "HEALTHY"
STATE_DEGRADED = "DEGRADED"
STATE_FINALIZING = "FINALIZING"
STATE_IDLE = "IDLE"          # enabled, healthy, no run currently active
STATE_FINALIZED = "FINALIZED"  # enabled, healthy, last run finalized, no run active

# Mission-execution states that mean "a real run is still in progress" across a
# restart (mirrors mission_execution_controller._LIVE_STATES; kept as a local
# literal so this module never imports the controller -- task section 43/49).
_LIVE_MISSION_STATES = frozenset(
    {"RUNNING", "PAUSED", "RETURNING_HOME", "HOME_ARRIVAL_PENDING"}
)

TELEMETRY_COLUMNS = [
    "timestamp_utc", "monotonic_time_s", "elapsed_s",
    # Vehicle identity, per sample (task: energy-calibration bundle
    # requirements) -- cheap, already known to the caller every iteration;
    # lets a run's telemetry.csv be identified/joined without cross-
    # referencing manifest.json.
    "vehicle_id",
    "latitude", "longitude",
    # Age (seconds) of the underlying MAVLink position/message evidence the
    # lat/lon on THIS row were read from (decision_snapshot.position_age_s,
    # reused verbatim) -- distinct from telemetry_age_s (heartbeat age) and
    # snapshot_age_s (recorder-sampler freshness) below: this is specifically
    # "how old was the GPS fix", needed to judge whether successive rows are
    # safe to treat as independent position samples for distance/speed work.
    "position_age_s",
    "altitude_m", "speed_m_s", "heading_deg",
    "mode", "armed",
    "current_waypoint", "mission_count",
    "physical_battery_percent", "battery_raw", "battery_valid",
    # Which source the ENERGY POLICY actually used this iteration -- reuses
    # mission_feasibility.py's own existing SOURCE_PHYSICAL/SOURCE_INJECTED
    # evidence (feasibility_result.battery_source) verbatim; never a second,
    # parallel labelling scheme. physical_battery_percent/injected_battery_
    # percent/policy_battery_percent above already keep the three values
    # distinct -- this column just names which one the policy consumed.
    "battery_source",
    "injected_battery_percent", "policy_battery_percent",
    # Raw physical battery voltage/current (decision_snapshot.battery_voltage/
    # battery_current, straight from vehicle telemetry -- see agent_state.py's
    # BATTERY_STATUS/SYS_STATUS merge on the vehicle Flask side). Experiment
    # injection (experiment_injection.py) only ever overrides battery_percent/
    # energy_margin_percent/communication_state -- it has no voltage/current
    # override -- so these two columns are ALWAYS the physical measurement,
    # never simulated, regardless of whether a battery_percent injection is
    # active this run. This is the raw evidence Q_meas/E_meas integration
    # (offline energy-calibration analysis) is computed from.
    "voltage_V", "current_A",
    "distance_to_home_m", "safe_return_distance_m", "usable_range_m",
    "return_cost_percent", "energy_margin_percent",
    "communication_state", "communication_source", "operator_reachable", "telemetry_age_s",
    # WireGuard handshake freshness evidence (E3 instrumentation task) --
    # copied verbatim from communication.wireguard_status()/_parse_wg_dump(),
    # the SAME evidence get_comm_state()/vpn_ok() already consult this
    # iteration. Observational only: nothing here re-derives or reinterprets
    # the 180s (WG_RECENT_HANDSHAKE_S) threshold, and no classifier/risk/
    # decision code reads these two columns back. wireguard_handshake_age_s
    # is None whenever the age isn't computable (no peer / never handshaken /
    # `wg` unavailable) -- never fabricated to 0. wireguard_fresh is True
    # only for status "RECENT_HANDSHAKE", False only for "STALE", and None
    # for every other status (DOWN/NO_HANDSHAKE/UNKNOWN), matching
    # _parse_wg_dump()'s own vocabulary exactly.
    "wireguard_handshake_age_s", "wireguard_fresh",
    "operator_contact_age_s", "buffer_usage",
    "control_authority", "autonomy_level",
    "mission_execution_state", "mission_execution_phase",
    "replan_state", "current_decision",
    # Source-freshness evidence for the latest-snapshot sampler (task section
    # 4/6): how old the underlying Local Agent snapshot was WHEN this sample
    # was taken, so a 2 Hz recorder cadence never silently implies a 2 Hz
    # source measurement rate -- see ExperimentRecorder._sample_once.
    "snapshot_generated_monotonic_s", "snapshot_age_s",
]

# Vehicle/mission fields that, when present (and non-None) on ANY recorded
# event's `data`, are treated as authoritative TERMINAL evidence for that run
# (task section 2) -- captured into _RunState.terminal_snapshot as the run's
# own controller proves them, so the final summary can derive final_mode/
# final_armed/final_authority/... from the same explicit evidence a
# terminal_reason string quotes, instead of whichever periodic telemetry
# sample happened to be queued last (the "authority -> OPERATOR" vs
# "final_authority: LOCAL_AGENT" contradiction this task exists to fix).
_TERMINAL_VEHICLE_FIELDS = (
    "final_mode", "final_armed", "final_authority",
    "mission_execution_state", "mission_execution_phase",
    "current_waypoint", "mission_count", "route_hash", "mission_id",
)
# Stop-specific terminal evidence (task section 2's "Stop evidence" list) --
# only ever populated from a STOP_COMPLETE event's own data, so it never
# collides with another event type's fields.
_STOP_EVIDENCE_FIELDS = (
    "hold_verified", "original_restored", "rewind_verified", "sequence_after",
    "replan_reset", "experiment_cleared", "authority_after", "ready_for_start",
)

# Bundle filenames considered for checksums.sha256 (task section 31) -- only
# those that actually exist in the run directory are hashed.
_CHECKSUM_CANDIDATES = [
    "manifest.json", "config_snapshot.json", "timeline.jsonl", "telemetry.csv",
    "decision_snapshots.jsonl", "annotations.jsonl", "original_mission.json",
    "planning_package.json", "summary.json",
]


def _now_iso(ts: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()


def _write_json_atomic(path: str, obj: Any) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True, default=str)
    os.replace(tmp, path)


def _append_jsonl(handle, obj: Any) -> None:
    handle.write(json.dumps(obj, sort_keys=True, default=str))
    handle.write("\n")


def _git_info(repo_dir: str) -> Dict[str, Any]:
    """Cheap, ONE-TIME git metadata capture (task section 8: never poll git
    repeatedly). Never raises -- an unavailable git binary/repo degrades to
    None fields, not a startup failure."""
    def _run(args):
        try:
            out = subprocess.run(
                args, cwd=repo_dir, capture_output=True, text=True, timeout=2,
            )
            if out.returncode != 0:
                return None
            return out.stdout.strip()
        except Exception:
            return None

    commit = _run(["git", "rev-parse", "HEAD"])
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    status = _run(["git", "status", "--porcelain"])
    return {
        "git_commit": commit,
        "git_branch": branch,
        "git_dirty": bool(status) if status is not None else None,
    }


class _RunState:
    """Writer-thread-only state for one open run. Never touched from any
    other thread -- the writer thread is the sole owner, so no lock is
    needed here (the small cross-thread facts live on ExperimentRecorder
    itself, guarded by ExperimentRecorder._lock)."""

    def __init__(self, run_id: str, run_dir: str, experiment_meta: dict, run_meta: dict):
        self.run_id = run_id
        self.dir = run_dir
        self.experiment_meta = experiment_meta
        self.run_meta = run_meta
        self.start_mono = run_meta.get("_start_mono", time.monotonic())
        self.handles: Dict[str, Any] = {}
        self.csv_writer = None
        self.csv_header_written = False
        self.revision_numbers_written = set()
        self.original_mission_meta: Dict[str, Any] = {}
        self.last_telemetry: Dict[str, Any] = {}
        self.last_decision_label = None
        # Authoritative terminal vehicle-state evidence (task section 2),
        # merged in from whichever named lifecycle event(s) actually proved
        # it (e.g. STOP_COMPLETE) -- see _update_counters_from_event. Wins
        # over last_telemetry in _write_terminal_summary when present.
        self.terminal_snapshot: Dict[str, Any] = {}
        # First-occurrence elapsed_s (monotonic, run-relative) of named
        # timeline milestones (task sections 8/9) -- e.g. "start_requested",
        # "first_running", "first_replan_trigger". Only ever set from events
        # actually written to this run's timeline; never fabricated.
        self.timing_marks: Dict[str, float] = {}
        self.comm_log: List[tuple] = []          # (mono_ts, state), one entry per
                                                  # OBSERVED communication_state (from
                                                  # every telemetry sample, densely --
                                                  # not only explicit transition events;
                                                  # E2 water-trial recorder-aggregation fix)
        self.counters: Dict[str, int] = {
            "decision_snapshot_count": 0, "decision_change_count": 0,
            "replan_attempt_count": 0, "revision_count": 0,
            "safe_hold_count": 0, "validation_failure_count": 0,
            "fallback_count": 0, "disconnect_count": 0, "reconnect_count": 0,
            # E2 water-trial integration task section 11: decision_change_count
            # (above) tracks ONLY decision_engine.py's rule-based battery/GPS/
            # mission-phase label -- it never reflected risk_model.py's
            # level/recommendation or replan_controller.py's own energy
            # decision. These three make each layer's change count explicit;
            # decision_change_count is kept, unchanged, for back-compat.
            "risk_level_change_count": 0, "recommendation_change_count": 0,
            "replan_decision_change_count": 0,
        }
        self.last_risk_level = None
        self.last_recommendation = None
        self.last_replan_decision = None
        self.errors: List[dict] = []
        self.last_flush_mono = time.monotonic()
        self.finalized = False


class ExperimentRecorder:
    def __init__(
        self,
        cfg: Optional[experiment_record_config.ExperimentRecordConfig] = None,
        vehicle_id: Optional[str] = None,
        clock=None,
        monotonic=None,
    ):
        self.cfg = cfg or experiment_record_config.DEFAULT
        self.vehicle_id = vehicle_id
        self._clock = clock or time.time
        self._mono = monotonic or time.monotonic

        # ── Small cross-thread state (guarded by _lock; NEVER held during I/O) ──
        self._lock = threading.Lock()
        self._run_id: Optional[str] = None
        self._run_active = False
        self._finalizing = False
        self._run_started_at: Optional[float] = None
        self._run_start_mono: Optional[float] = None
        self._experiment_meta: Dict[str, Any] = {}
        self._next_run_meta: Dict[str, Any] = {}
        self._degraded = False
        self._last_error: Optional[dict] = None
        self._dropped_telemetry = 0
        self._dropped_event = 0
        self._dropped_decision = 0
        self._enqueue_count = 0
        self._max_queue_depth = 0
        self._writer_error_count = 0
        self._last_finalized_run_id: Optional[str] = None
        self._restart_reopened = False

        self._repo_dir = os.path.dirname(os.path.abspath(__file__))
        self._software_info = None  # lazily captured once, on first start_run

        self._high_q: Optional["queue.Queue"] = None
        self._low_q: Optional["queue.Queue"] = None
        self._writer_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # Writer-thread-only map of currently open runs (see _RunState).
        self._w_open_runs: Dict[str, _RunState] = {}

        # ── Latest-snapshot telemetry sampler (task section 4) ───────────────
        # The Local Agent main loop hands off its already-computed state here
        # (update_latest_telemetry_snapshot) every iteration -- a cheap dict
        # replace under a tiny lock, no I/O, returns immediately. A single
        # long-lived sampler thread copies the latest snapshot at the
        # configured telemetry_hz cadence and enqueues it as a normal
        # low-priority telemetry record, completely decoupled from the main
        # loop's own (much slower, I/O-bound) cadence.
        self._snapshot_lock = threading.Lock()
        self._latest_snapshot: Optional[Dict[str, Any]] = None
        self._latest_snapshot_mono: Optional[float] = None
        self._sampler_thread: Optional[threading.Thread] = None

        if self.cfg.experiment_recording_enabled:
            self._high_q = queue.Queue(maxsize=max(1, self.cfg.experiment_record_queue_capacity))
            self._low_q = queue.Queue(maxsize=max(1, self.cfg.experiment_record_low_queue_capacity))
            self._start_writer()
            self._start_sampler()

    # ── Writer thread lifecycle ──────────────────────────────────────────────
    def _start_writer(self) -> None:
        self._writer_thread = threading.Thread(
            target=self._writer_loop, name="experiment-recorder-writer", daemon=True,
        )
        self._writer_thread.start()

    def _start_sampler(self) -> None:
        self._sampler_thread = threading.Thread(
            target=self._sampler_loop, name="experiment-recorder-sampler", daemon=True,
        )
        self._sampler_thread.start()

    def shutdown(self, timeout: float = 2.0) -> None:
        """Best-effort, bounded stop -- test/process-exit convenience only.
        Never called from the mission/replan critical path."""
        self._stop_event.set()
        if self._sampler_thread is not None:
            self._sampler_thread.join(timeout=timeout)
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=timeout)

    # ── Latest-snapshot handoff (caller-thread side; cheap, no I/O) ──────────
    def update_latest_telemetry_snapshot(self, sample: dict) -> None:
        """Called from the Local Agent main loop, every iteration, with
        whatever telemetry fields it already computed this iteration (task
        section 4). Cheap dict copy + lock swap, no I/O, no queue, never
        throttled here and never blocks -- the SAMPLER decides when/whether
        to actually record a sample, at its own configured cadence,
        independent of however fast or slow this is called."""
        if not self.cfg.experiment_recording_enabled:
            return
        try:
            snap = dict(sample) if sample else {}
            mono = self._mono()
            with self._snapshot_lock:
                self._latest_snapshot = snap
                self._latest_snapshot_mono = mono
        except Exception:
            pass

    # ── Sampler thread -- reads the latest snapshot + enqueues only ─────────
    def _sampler_loop(self) -> None:
        """One long-lived thread (task section 5): sleeps at roughly
        1/telemetry_hz cadence (monotonic-paced, not hard real-time), and on
        each wake copies the latest in-memory snapshot and enqueues it as a
        telemetry record via the SAME record_telemetry() path callers already
        use (same envelope, same low-priority bounded queue, same drop
        counter under pressure). Performs NO I/O of its own: no HTTP, no
        Pixhawk/MAVLink, no disk, no mission/replan calls -- only an
        in-memory dict copy and a queue.put_nowait()."""
        while not self._stop_event.is_set():
            hz = self.cfg.experiment_record_telemetry_hz
            if hz <= 0:
                # Sampling disabled by configuration -- sleep coarsely rather
                # than busy-looping; still wakes promptly on shutdown.
                self._stop_event.wait(1.0)
                continue
            period = 1.0 / hz
            cycle_start = self._mono()
            self._sample_once()
            elapsed = self._mono() - cycle_start
            remaining = period - elapsed
            if remaining > 0:
                self._stop_event.wait(remaining)

    def _sample_once(self) -> None:
        if not self._run_ready():
            return
        with self._snapshot_lock:
            snap = self._latest_snapshot
            snap_mono = self._latest_snapshot_mono
        if snap is None:
            # No producer snapshot has ever been handed off -- skip rather
            # than fabricate a sample (task section 4).
            return
        now_mono = self._mono()
        age = None if snap_mono is None else max(0.0, now_mono - snap_mono)
        sample = dict(snap)
        # Honest source-freshness evidence (task section 6): this sample may
        # repeat the same underlying source values across several recorder
        # ticks if the main loop hasn't produced a fresh one yet -- expose
        # that explicitly instead of implying a source measurement happened
        # at recorder cadence.
        sample["snapshot_generated_monotonic_s"] = None if snap_mono is None else round(snap_mono, 3)
        sample["snapshot_age_s"] = None if age is None else round(age, 3)
        self.record_telemetry(sample)

    # ── Experiment metadata configuration (task section 4) ──────────────────
    def configure_next_run(self, patch: dict) -> dict:
        """Descriptive only -- never touches vehicle/impairment state. Merges
        into the metadata the NEXT start_run() call will bind to this run."""
        if not isinstance(patch, dict):
            return self.get_next_run_config()
        with self._lock:
            for key in ("experiment_id", "experiment_type", "trial_number", "scenario", "notes"):
                if key in patch:
                    self._next_run_meta[key] = patch[key]
            return dict(self._next_run_meta)

    def get_next_run_config(self) -> dict:
        with self._lock:
            return dict(self._next_run_meta)

    # ── Session lifecycle ────────────────────────────────────────────────────
    def start_run(
        self,
        vehicle_id: Optional[str] = None,
        mission_id: Optional[str] = None,
        original_route_hash: Optional[str] = None,
        original_mission: Optional[dict] = None,
        planning_package: Optional[dict] = None,
    ) -> Optional[str]:
        """Allocate a new run and enqueue its bundle-open job. Fast, never
        blocks, never raises. Returns the new run_id (or None if disabled/
        failed) purely for optional caller logging -- nothing downstream
        depends on this return value."""
        if not self.cfg.experiment_recording_enabled:
            return None
        try:
            now = self._clock()
            mono = self._mono()
            short = uuid.uuid4().hex[:8]
            vid = vehicle_id or self.vehicle_id or "usv"
            ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
            run_id = f"run-{ts}-{vid}-{short}"
            with self._lock:
                experiment_meta = dict(self._next_run_meta) or {}
                experiment_meta.setdefault("experiment_id", None)
                experiment_meta.setdefault("experiment_type", "UNSPECIFIED")
                experiment_meta.setdefault("trial_number", None)
                experiment_meta.setdefault("scenario", None)
                experiment_meta.setdefault("notes", None)
                self._run_id = run_id
                self._run_active = True
                self._finalizing = False
                self._run_started_at = now
                self._run_start_mono = mono
                self._experiment_meta = experiment_meta
                self._restart_reopened = False
                self._dropped_telemetry = 0
                self._dropped_event = 0
                self._dropped_decision = 0

            if self._software_info is None:
                self._software_info = _git_info(self._repo_dir)

            run_meta = {
                "run_id": run_id, "vehicle_id": vid, "mission_id": mission_id,
                "original_route_hash": original_route_hash,
                "start_time_utc": _now_iso(now), "start_monotonic_s": round(mono, 3),
                "_start_mono": mono,
            }
            job = {
                "_kind": "start_run", "run_id": run_id,
                "dir": os.path.join(self.cfg.experiment_record_directory, run_id),
                "experiment_meta": experiment_meta, "run_meta": run_meta,
                "software": self._software_info,
                "original_mission": original_mission, "planning_package": planning_package,
                "config_snapshot": self._gather_config_snapshot(),
            }
            self._enqueue(job, priority="high")
            return run_id
        except Exception as e:  # pragma: no cover - defensive, must never raise
            self._degrade_sync("START_RUN_FAILED", str(e))
            return None

    def finalize_async(self, outcome: Optional[str], reason: Optional[str] = None) -> None:
        """Fast, non-blocking. No-op if no run is active. Mission/replan
        lifecycle NEVER waits for this -- see task section 32."""
        if not self.cfg.experiment_recording_enabled:
            return
        try:
            with self._lock:
                if not self._run_active or self._run_id is None:
                    return
                run_id = self._run_id
                self._finalizing = True
                self._run_active = False
                self._run_id = None
            self._enqueue({
                "_kind": "finalize", "run_id": run_id, "outcome": outcome,
                "reason": reason, "finalize_time_utc": _now_iso(self._clock()),
                "finalize_mono": self._mono(),
            }, priority="high")
        except Exception as e:  # pragma: no cover
            self._degrade_sync("FINALIZE_FAILED", str(e))

    def reconcile_after_restart(self, mission_exec_status: Optional[dict]) -> None:
        """Called ONCE at process startup, after mission-execution's own
        recover_after_restart() has settled (task section 6). Reads the tiny
        pointer file synchronously (one-time bootstrap read, same idiom as
        every other *StatusStore.load_into in this codebase) then enqueues
        the appropriate reopen/finalize job on the writer thread -- no run
        bundle is ever deleted or truncated."""
        if not self.cfg.experiment_recording_enabled:
            return
        try:
            pointer_path = self._pointer_path()
            if not os.path.exists(pointer_path):
                return
            with open(pointer_path, "r") as f:
                pointer = json.load(f)
            if not isinstance(pointer, dict) or pointer.get("state") not in ("ACTIVE", "FINALIZING"):
                return
            run_id = pointer.get("run_id")
            if not run_id:
                return
            state = (mission_exec_status or {}).get("state")
            live = state in _LIVE_MISSION_STATES
            run_dir = os.path.join(self.cfg.experiment_record_directory, run_id)
            if live:
                now = self._clock()
                mono = self._mono()
                with self._lock:
                    self._run_id = run_id
                    self._run_active = True
                    self._finalizing = False
                    self._run_started_at = pointer.get("started_at_wall", now)
                    self._run_start_mono = mono  # monotonic resets across a restart
                    self._experiment_meta = pointer.get("experiment_meta") or {}
                    self._restart_reopened = True
                self._enqueue({
                    "_kind": "reopen_run", "run_id": run_id, "dir": run_dir,
                    "experiment_meta": self._experiment_meta,
                    "prior_started_at_utc": pointer.get("started_at_wall_iso"),
                    "reopened_at_utc": _now_iso(now), "reopened_mono": mono,
                }, priority="high")
            else:
                self._enqueue({
                    "_kind": "finalize", "run_id": run_id, "outcome": "INTERRUPTED",
                    "reason": ("Local Agent process restarted and mission execution did "
                               f"not reconcile to a live state (state={state!r}); "
                               "run finalized as INTERRUPTED, never resumed."),
                    "finalize_time_utc": _now_iso(self._clock()),
                    "finalize_mono": self._mono(),
                }, priority="high")
        except Exception as e:  # pragma: no cover - restart bootstrap must never raise
            self._degrade_sync("RESTART_RECONCILE_FAILED", str(e))

    # ── Recording API (task section 44) ──────────────────────────────────────
    def record_event(self, event_type: str, source: str, data: Optional[dict] = None,
                     priority: str = "high") -> None:
        if not self._run_ready():
            return
        try:
            rec = self._envelope("event")
            rec["type"] = event_type
            rec["source"] = source
            rec["data"] = data or {}
            self._enqueue({"_kind": "event", "run_id": rec["run_id"], "record": rec},
                          priority=priority, drop_counter="_dropped_event")
        except Exception:
            pass

    def record_decision(self, snapshot: dict) -> None:
        if not self._run_ready():
            return
        try:
            rec = self._envelope("decision")
            rec.update(snapshot or {})
            self._enqueue({"_kind": "decision", "run_id": rec["run_id"], "record": rec},
                          priority="high", drop_counter="_dropped_decision")
        except Exception:
            pass

    def record_telemetry(self, sample: dict) -> None:
        if not self._run_ready():
            return
        try:
            rec = self._envelope("telemetry")
            rec.update(sample or {})
            self._enqueue({"_kind": "telemetry", "run_id": rec["run_id"], "record": rec},
                          priority="low", drop_counter="_dropped_telemetry")
        except Exception:
            pass

    def record_annotation(self, category: str, name: str, data: Optional[dict] = None,
                          source: Optional[str] = None) -> None:
        if not self._run_ready():
            return
        try:
            rec = self._envelope("annotation")
            rec["category"] = category
            rec["name"] = name
            rec["source"] = source
            rec["data"] = data or {}
            self._enqueue({"_kind": "annotation", "run_id": rec["run_id"], "record": rec},
                          priority="high")
        except Exception:
            pass

    def record_revision(self, revision: dict) -> None:
        if not self._run_ready() or not isinstance(revision, dict):
            return
        try:
            with self._lock:
                run_id = self._run_id
            self._enqueue({"_kind": "revision", "run_id": run_id, "revision": revision},
                          priority="high")
        except Exception:
            pass

    # ── Status (task section 26 -- read-only, fast, lock-light) ─────────────
    def status(self) -> dict:
        if not self.cfg.experiment_recording_enabled:
            return {"enabled": False, "active": False, "recorder_state": STATE_DISABLED}
        with self._lock:
            active = self._run_active
            run_id = self._run_id
            experiment_meta = dict(self._experiment_meta)
            started_at = self._run_started_at
            start_mono = self._run_start_mono
            finalizing = self._finalizing
            degraded = self._degraded
            last_error = self._last_error
            dropped_t = self._dropped_telemetry
            dropped_e = self._dropped_event
            dropped_d = self._dropped_decision
            enqueue_count = self._enqueue_count
            max_depth = self._max_queue_depth
            writer_errors = self._writer_error_count
            last_finalized = self._last_finalized_run_id
            restart_reopened = self._restart_reopened
        queue_depth = 0
        queue_capacity = self.cfg.experiment_record_queue_capacity + self.cfg.experiment_record_low_queue_capacity
        if self._high_q is not None and self._low_q is not None:
            queue_depth = self._high_q.qsize() + self._low_q.qsize()
        elapsed_s = None
        if active and start_mono is not None:
            elapsed_s = round(self._mono() - start_mono, 2)
        if degraded:
            recorder_state = STATE_DEGRADED
        elif finalizing:
            recorder_state = STATE_FINALIZING
        elif active:
            recorder_state = STATE_HEALTHY
        elif last_finalized is not None:
            recorder_state = STATE_FINALIZED
        else:
            recorder_state = STATE_IDLE
        return {
            "enabled": True,
            "active": active,
            "recorder_state": recorder_state,
            "experiment_id": experiment_meta.get("experiment_id"),
            "experiment_type": experiment_meta.get("experiment_type"),
            "trial_number": experiment_meta.get("trial_number"),
            "run_id": run_id,
            "directory": self.cfg.experiment_record_directory,
            "started_at": None if started_at is None else _now_iso(started_at),
            "elapsed_s": elapsed_s,
            "finalizing": finalizing,
            "restart_reopened": restart_reopened,
            "queue_depth": queue_depth,
            "queue_capacity": queue_capacity,
            "dropped_telemetry_records": dropped_t,
            "dropped_event_records": dropped_e,
            "dropped_decision_records": dropped_d,
            "enqueue_count": enqueue_count,
            "maximum_queue_depth": max_depth,
            "writer_error_count": writer_errors,
            "degraded": degraded,
            "last_error": last_error,
            "last_finalized_run_id": last_finalized,
        }

    def list_runs(self) -> List[dict]:
        """Bounded metadata-only listing (task section 26) -- reads manifest.json
        of each run directory. Best-effort; a run whose manifest can't be read
        is skipped, never raises."""
        out = []
        try:
            base = self.cfg.experiment_record_directory
            if not os.path.isdir(base):
                return out
            for name in sorted(os.listdir(base)):
                if not name.startswith("run-"):
                    continue
                manifest_path = os.path.join(base, name, "manifest.json")
                if not os.path.exists(manifest_path):
                    continue
                try:
                    with open(manifest_path, "r") as f:
                        manifest = json.load(f)
                except Exception:
                    continue
                out.append({
                    "run_id": name,
                    "experiment": manifest.get("experiment"),
                    "run": manifest.get("run"),
                    "finalized": os.path.exists(os.path.join(base, name, "summary.json")),
                })
        except Exception:
            pass
        return out

    def get_run(self, run_id: str) -> Optional[dict]:
        try:
            base = self.cfg.experiment_record_directory
            run_dir = os.path.join(base, run_id)
            manifest_path = os.path.join(run_dir, "manifest.json")
            if not os.path.exists(manifest_path):
                return None
            with open(manifest_path, "r") as f:
                manifest = json.load(f)
            summary = None
            summary_path = os.path.join(run_dir, "summary.json")
            if os.path.exists(summary_path):
                try:
                    with open(summary_path, "r") as f:
                        summary = json.load(f)
                except Exception:
                    summary = None
            return {"run_id": run_id, "manifest": manifest, "summary": summary,
                    "directory": run_dir}
        except Exception:
            return None

    # ── Internal helpers (caller-thread side; cheap, no I/O) ─────────────────
    def _run_ready(self) -> bool:
        if not self.cfg.experiment_recording_enabled:
            return False
        with self._lock:
            return self._run_active and self._run_id is not None

    def _envelope(self, kind: str) -> dict:
        with self._lock:
            run_id = self._run_id
            experiment_meta = self._experiment_meta
            start_mono = self._run_start_mono
        now = self._clock()
        mono = self._mono()
        return {
            "schema_version": EVENT_SCHEMA_VERSION,
            "kind": kind,
            "experiment_id": experiment_meta.get("experiment_id"),
            "run_id": run_id,
            "timestamp_utc": _now_iso(now),
            "monotonic_time_s": round(mono, 3),
            "elapsed_s": None if start_mono is None else round(mono - start_mono, 3),
        }

    def _gather_config_snapshot(self) -> dict:
        """Whitelisted, bounded runtime configuration (task section 9/46) --
        never environment variables, headers, or secrets wholesale."""
        snapshot: Dict[str, Any] = {"recorder": {
            "enabled": self.cfg.experiment_recording_enabled,
            "telemetry_hz": self.cfg.experiment_record_telemetry_hz,
            "queue_capacity": self.cfg.experiment_record_queue_capacity,
            "low_queue_capacity": self.cfg.experiment_record_low_queue_capacity,
            "directory": self.cfg.experiment_record_directory,
        }}
        try:
            import mission_execution_config as me_config
            resolved, sources = me_config.resolve()
            snapshot["mission_execution"] = {"values": resolved.to_dict(), "sources": sources}
        except Exception:
            snapshot["mission_execution"] = None
        try:
            import replan_config
            resolved, sources = replan_config.resolve()
            snapshot["replan"] = {"values": resolved.to_dict(), "sources": sources}
        except Exception:
            snapshot["replan"] = None
        try:
            import config as agent_config
            snapshot["communication"] = {
                "connected_interval_s": getattr(agent_config, "CONNECTED_INTERVAL", None),
                "partitioned_interval_s": getattr(agent_config, "PARTITIONED_INTERVAL", None),
                "disconnected_interval_s": getattr(agent_config, "DISCONNECTED_INTERVAL", None),
                "operator_connect_timeout_s": getattr(agent_config, "OPERATOR_CONNECT_TIMEOUT", None),
                "operator_read_timeout_s": getattr(agent_config, "OPERATOR_READ_TIMEOUT", None),
                "battery_rtl_threshold_percent": getattr(agent_config, "BATTERY_RTL_THRESHOLD_PERCENT", None),
                "gps_min_fix_type": getattr(agent_config, "GPS_MIN_FIX_TYPE", None),
                "mavlink_heartbeat_timeout_s": getattr(agent_config, "MAVLINK_HEARTBEAT_TIMEOUT_S", None),
            }
        except Exception:
            snapshot["communication"] = None
        return snapshot

    def _enqueue(self, job: dict, priority: str, drop_counter: Optional[str] = None) -> None:
        q = self._high_q if priority == "high" else self._low_q
        if q is None:
            return
        try:
            q.put_nowait(job)
            with self._lock:
                self._enqueue_count += 1
                depth = (self._high_q.qsize() if self._high_q else 0) + \
                        (self._low_q.qsize() if self._low_q else 0)
                if depth > self._max_queue_depth:
                    self._max_queue_depth = depth
        except queue.Full:
            with self._lock:
                if drop_counter == "_dropped_telemetry":
                    self._dropped_telemetry += 1
                elif drop_counter == "_dropped_decision":
                    self._dropped_decision += 1
                elif drop_counter == "_dropped_event":
                    self._dropped_event += 1
                elif priority == "high":
                    # A high-priority record with no explicit counter (e.g. a
                    # revision/annotation/finalize job) still must not block or
                    # silently vanish unaccounted -- count it as a dropped event.
                    self._dropped_event += 1

    def _degrade_sync(self, code: str, message: str) -> None:
        with self._lock:
            self._degraded = True
            self._last_error = {"code": code, "message": message}

    def _pointer_path(self) -> str:
        return os.path.join(self.cfg.experiment_record_directory, ".active_run.json")

    # ══════════════════════════════════════════════════════════════════════
    # Writer thread -- the ONLY code below this line ever touches disk.
    # ══════════════════════════════════════════════════════════════════════
    def _writer_loop(self) -> None:
        while True:
            job = None
            try:
                job = self._high_q.get(timeout=self.cfg.writer_poll_interval_s)
            except queue.Empty:
                try:
                    job = self._low_q.get_nowait()
                except queue.Empty:
                    job = None
            except Exception:
                job = None

            if job is None:
                self._writer_periodic_flush()
                if self._stop_event.is_set() and self._high_q.empty() and self._low_q.empty():
                    break
                continue

            try:
                self._dispatch(job)
            except Exception as e:
                self._writer_degrade(None, "WRITE_FAILED", f"{type(e).__name__}: {e}")

        # Bounded final flush + handle close on a graceful stop (task section
        # 25/32) -- never runs on the mission/replan critical path
        # (shutdown() is only called from tests / deliberate process
        # shutdown, never from a controller). A run stopped here without
        # having reached finalize_async() stays legitimately un-finalized on
        # disk -- the next process start's reconcile_after_restart() decides
        # its fate, same as any other interruption.
        for rs in list(self._w_open_runs.values()):
            self._flush_run(rs)
            for handle in rs.handles.values():
                try:
                    handle.close()
                except OSError:
                    pass

    def _writer_degrade(self, run_id: Optional[str], code: str, message: str) -> None:
        with self._lock:
            self._degraded = True
            self._last_error = {"code": code, "message": message}
            self._writer_error_count += 1
        rs = self._w_open_runs.get(run_id) if run_id else None
        if rs is not None:
            rs.errors.append({"code": code, "message": message, "at": _now_iso(self._clock())})
            del rs.errors[:-20]

    def _dispatch(self, job: dict) -> None:
        kind = job.get("_kind")
        if kind == "start_run":
            self._w_start_run(job)
        elif kind == "reopen_run":
            self._w_reopen_run(job)
        elif kind == "event":
            self._w_write_line(job, "timeline")
        elif kind == "decision":
            self._w_write_decision(job)
        elif kind == "telemetry":
            self._w_write_telemetry(job)
        elif kind == "annotation":
            self._w_write_line(job, "annotations")
        elif kind == "revision":
            self._w_write_revision(job)
        elif kind == "finalize":
            self._w_finalize(job)
        # Unknown kinds are ignored -- a malformed job must never kill the
        # writer thread (task section 25).

    def _open_run_files(self, rs: _RunState, mode: str) -> None:
        for name, fname in (("timeline", "timeline.jsonl"),
                            ("decision", "decision_snapshots.jsonl"),
                            ("annotations", "annotations.jsonl")):
            rs.handles[name] = open(os.path.join(rs.dir, fname), mode)
        csv_path = os.path.join(rs.dir, "telemetry.csv")
        is_new = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
        rs.handles["telemetry_csv"] = open(csv_path, mode, newline="")
        rs.csv_writer = csv.DictWriter(rs.handles["telemetry_csv"], fieldnames=TELEMETRY_COLUMNS,
                                       extrasaction="ignore")
        if is_new:
            rs.csv_writer.writeheader()
            rs.handles["telemetry_csv"].flush()

    def _w_start_run(self, job: dict) -> None:
        run_id = job["run_id"]
        run_dir = job["dir"]
        try:
            os.makedirs(run_dir, exist_ok=True)
        except OSError as e:
            self._writer_degrade(None, "MKDIR_FAILED", str(e))
            return
        rs = _RunState(run_id, run_dir, job["experiment_meta"], job["run_meta"])
        self._w_open_runs[run_id] = rs

        run_meta_out = {k: v for k, v in job["run_meta"].items() if not k.startswith("_")}
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "experiment": job["experiment_meta"],
            "run": run_meta_out,
            "software": job.get("software") or {},
            "source": SOURCE_LABEL,
        }
        try:
            _write_json_atomic(os.path.join(run_dir, "manifest.json"), manifest)
            _write_json_atomic(os.path.join(run_dir, "config_snapshot.json"), job.get("config_snapshot") or {})
            if job.get("original_mission") is not None:
                _write_json_atomic(os.path.join(run_dir, "original_mission.json"), job["original_mission"])
                om = job["original_mission"]
                if isinstance(om, dict):
                    rs.original_mission_meta = {
                        "mission_id": om.get("mission_id"),
                        "original_route_hash": om.get("route_hash") or om.get("original_route_hash"),
                        "original_waypoint_count": len(om.get("route") or []) or None,
                    }
            if job.get("planning_package") is not None:
                _write_json_atomic(os.path.join(run_dir, "planning_package.json"), job["planning_package"])
            self._open_run_files(rs, "a")
            run_started_record = {
                "schema_version": EVENT_SCHEMA_VERSION, "run_id": run_id,
                "timestamp_utc": run_meta_out.get("start_time_utc"),
                "monotonic_time_s": run_meta_out.get("start_monotonic_s"),
                "elapsed_s": 0.0, "type": "RECORDER_RUN_STARTED", "source": "experiment_recorder",
                "data": {"experiment_type": job["experiment_meta"].get("experiment_type")},
            }
            _append_jsonl(rs.handles["timeline"], run_started_record)
            # Written directly (not via _w_write_line/_dispatch), so hook the
            # same counters/timing-marks update explicitly (task section 8) --
            # this is the run's t=0.000 anchor for start_to_running_s.
            self._update_counters_from_event(rs, run_started_record)
            self._flush_run(rs)
            self._write_pointer(run_id, "ACTIVE", job["experiment_meta"], job["run_meta"])
        except OSError as e:
            self._writer_degrade(run_id, "WRITE_FAILED", str(e))

    def _w_reopen_run(self, job: dict) -> None:
        run_id = job["run_id"]
        run_dir = job["dir"]
        try:
            os.makedirs(run_dir, exist_ok=True)
        except OSError as e:
            self._writer_degrade(None, "MKDIR_FAILED", str(e))
            return
        run_meta = {"run_id": run_id, "_start_mono": job["reopened_mono"]}
        rs = _RunState(run_id, run_dir, job["experiment_meta"], run_meta)
        self._w_open_runs[run_id] = rs
        try:
            self._open_run_files(rs, "a")
            _append_jsonl(rs.handles["timeline"], {
                "schema_version": EVENT_SCHEMA_VERSION, "run_id": run_id,
                "timestamp_utc": job["reopened_at_utc"], "monotonic_time_s": round(job["reopened_mono"], 3),
                "elapsed_s": 0.0, "type": "PROCESS_RESTART_RECOVERY", "source": "experiment_recorder",
                "data": {"prior_started_at_utc": job.get("prior_started_at_utc"),
                        "note": "Local Agent process restarted; mission execution reconciled to a "
                                "live state, so this run was reopened append-only rather than "
                                "finalized. Elapsed-time accounting resets to this point (monotonic "
                                "clocks do not survive a process restart)."},
            })
            self._flush_run(rs)
            self._write_pointer(run_id, "ACTIVE", job["experiment_meta"], {})
        except OSError as e:
            self._writer_degrade(run_id, "WRITE_FAILED", str(e))

    def _w_write_line(self, job: dict, stream: str) -> None:
        run_id = job.get("run_id")
        rs = self._w_open_runs.get(run_id)
        if rs is None:
            return
        record = job["record"]
        handle = rs.handles.get(stream)
        if handle is None:
            return
        try:
            _append_jsonl(handle, record)
            if stream == "timeline":
                self._update_counters_from_event(rs, record)
        except OSError as e:
            self._writer_degrade(run_id, "WRITE_FAILED", str(e))

    def _update_counters_from_event(self, rs: _RunState, record: dict) -> None:
        etype = str(record.get("type") or "")
        data = record.get("data") or {}
        elapsed = record.get("elapsed_s")

        # ── Terminal vehicle-state evidence (task section 2) ──────────────
        # Merge whichever of the whitelisted fields THIS event actually
        # proved -- never overwrite a known value with an absent/None one,
        # and never invent a field the event didn't carry.
        for field in _TERMINAL_VEHICLE_FIELDS:
            if field in data and data[field] is not None:
                rs.terminal_snapshot[field] = data[field]
        if etype == "STOP_COMPLETE":
            evidence = {f: data[f] for f in _STOP_EVIDENCE_FIELDS if f in data}
            if evidence:
                rs.terminal_snapshot["stop_evidence"] = evidence

        # ── Timing milestones (task sections 8/9) -- first occurrence only ──
        if elapsed is not None:
            def _mark(key: str) -> None:
                if key not in rs.timing_marks:
                    rs.timing_marks[key] = elapsed

            if etype in ("RECORDER_RUN_STARTED", "MISSION_START_REQUESTED"):
                _mark("start_requested")
            if etype == "MISSION_EXECUTION_STATE_CHANGED" and data.get("to") == "RUNNING":
                _mark("first_running")
            if etype == "REPLAN_STATE_CHANGED":
                # E2 water-trial integration task section 11: the first
                # replan-FSM transition of any kind (distinct from the more
                # specific first_replan_trigger/_hold_confirmed/... marks
                # below, which name a SPECIFIC state).
                _mark("first_replan_transition_at")
                to = data.get("to")
                # "first trigger" == the first observable evidence the replan
                # controller reacted to a genuine trigger: the transaction's
                # first HOLD_REQUESTED. There is no earlier discrete
                # "trigger detected" timeline event to anchor to (task
                # section 9: derive only from named events already
                # recorded) -- see FINAL REPORT section C for this
                # interpretation.
                if to == "HOLD_REQUESTED":
                    _mark("first_replan_trigger")
                elif to == "HOLD_CONFIRMED":
                    _mark("first_replan_hold_confirmed")
                elif to == "MONITORING_REVISED":
                    _mark("first_replan_revised_auto")
                elif to in ("SUSPENDED", "FAILED", "SAFE_HOLD", "FALLBACK_RTL"):
                    _mark("first_replan_terminal")

        if "COMMUNICATION" in etype:
            new_state = data.get("to")
            # rs.comm_log itself is now fed exclusively from the DENSE
            # per-telemetry-sample communication_state in _w_write_telemetry
            # (E2 water-trial recorder-aggregation fix) -- not from this
            # sparse transition event, so a HIGH-priority event record can
            # never be interleaved ahead of an earlier-timestamped but
            # LOW-priority telemetry record and break the log's chronological
            # ordering that _compute_comm_durations relies on. This block
            # still owns disconnect_count/reconnect_count, unrelated to
            # comm_log.
            if new_state == "DISCONNECTED":
                rs.counters["disconnect_count"] += 1
            if data.get("from") in ("DISCONNECTED", "PARTITIONED") and new_state == "CONNECTED":
                rs.counters["reconnect_count"] += 1
        if "SAFE_HOLD" in etype:
            rs.counters["safe_hold_count"] += 1
        if "VALIDATION_FAILED" in etype or etype == "REPLAN_VALIDATION_FAILED":
            rs.counters["validation_failure_count"] += 1
        if "FALLBACK_RTL" in etype:
            rs.counters["fallback_count"] += 1
        if etype in ("REPLAN_ATTEMPT_FAILED",) or "REPLAN_STATE_CHANGED" in etype:
            if data.get("to") == "PLANNING":
                rs.counters["replan_attempt_count"] += 1

    def _w_write_decision(self, job: dict) -> None:
        run_id = job.get("run_id")
        rs = self._w_open_runs.get(run_id)
        if rs is None:
            return
        record = job["record"]
        handle = rs.handles.get("decision")
        if handle is None:
            return
        try:
            _append_jsonl(handle, record)
            rs.counters["decision_snapshot_count"] += 1
            label = record.get("current_decision")
            if label is not None and label != rs.last_decision_label:
                if rs.last_decision_label is not None:
                    rs.counters["decision_change_count"] += 1
                rs.last_decision_label = label

            # ── Three-layer change counts + first-occurrence marks (E2
            # water-trial integration task sections 11/12) -- risk LEVEL,
            # mission-level RECOMMENDATION, and the replan controller's OWN
            # energy decision are three distinct signals, each tracked
            # separately from decision_engine's label above.
            elapsed = record.get("elapsed_s")
            risk = record.get("risk") or {}
            risk_level = risk.get("level")
            if risk_level is not None and risk_level != rs.last_risk_level:
                if rs.last_risk_level is not None:
                    rs.counters["risk_level_change_count"] += 1
                rs.last_risk_level = risk_level
                if elapsed is not None and risk_level not in ("LOW", None) \
                        and "first_risk_escalation_at" not in rs.timing_marks:
                    rs.timing_marks["first_risk_escalation_at"] = elapsed

            recommendation = risk.get("recommendation")
            if recommendation is not None and recommendation != rs.last_recommendation:
                if rs.last_recommendation is not None:
                    rs.counters["recommendation_change_count"] += 1
                rs.last_recommendation = recommendation
                if elapsed is not None and recommendation == "RETURN_HOME" \
                        and "first_return_recommendation_at" not in rs.timing_marks:
                    rs.timing_marks["first_return_recommendation_at"] = elapsed

            replan_decision = record.get("replan_decision")
            if replan_decision is not None and replan_decision != rs.last_replan_decision:
                if rs.last_replan_decision is not None:
                    rs.counters["replan_decision_change_count"] += 1
                rs.last_replan_decision = replan_decision

            action_request = record.get("action_request") or {}
            if elapsed is not None and action_request.get("action") == "REQUEST_RETURN_HOME" \
                    and "first_return_action_request_at" not in rs.timing_marks:
                rs.timing_marks["first_return_action_request_at"] = elapsed
        except OSError as e:
            self._writer_degrade(run_id, "WRITE_FAILED", str(e))

    def _w_write_telemetry(self, job: dict) -> None:
        run_id = job.get("run_id")
        rs = self._w_open_runs.get(run_id)
        if rs is None or rs.csv_writer is None:
            return
        record = job["record"]
        try:
            rs.csv_writer.writerow(record)
            rs.last_telemetry = record
            # E2 water-trial recorder-aggregation fix: connected_duration_s
            # (_compute_comm_durations below) previously summed ONLY sparse
            # explicit COMMUNICATION_STATE_CHANGED transition events -- a run
            # with zero transitions (the common case) reported 0.0 instead of
            # the full duration. Every telemetry sample already carries
            # communication_state (local_agent.py, sampled continuously at
            # experiment_record_telemetry_hz, independent of transitions), so
            # feed rs.comm_log from there instead: append only on an actual
            # change (collapsing consecutive identical samples), with the
            # FIRST entry ever seeded at rs.start_mono (not the sample's own
            # timestamp) so the pre-first-sample gap is correctly attributed
            # rather than silently dropped. The aggregation math in
            # _compute_comm_durations is unchanged -- it was already correct
            # given a properly-seeded, densely-updated log.
            comm_state = record.get("communication_state")
            if comm_state is not None:
                if not rs.comm_log:
                    rs.comm_log.append((rs.start_mono, comm_state))
                elif rs.comm_log[-1][1] != comm_state:
                    mono = record.get("monotonic_time_s")
                    if mono is not None:
                        rs.comm_log.append((mono, comm_state))
        except (OSError, ValueError) as e:
            self._writer_degrade(run_id, "WRITE_FAILED", str(e))

    def _w_write_revision(self, job: dict) -> None:
        run_id = job.get("run_id")
        rs = self._w_open_runs.get(run_id)
        if rs is None:
            return
        revision = job["revision"]
        n = revision.get("new_revision")
        if n is None or n in rs.revision_numbers_written:
            return
        try:
            _write_json_atomic(os.path.join(rs.dir, f"revised_mission_r{n}.json"), revision)
            rs.revision_numbers_written.add(n)
            rs.counters["revision_count"] = len(rs.revision_numbers_written)
        except OSError as e:
            self._writer_degrade(run_id, "WRITE_FAILED", str(e))

    def _flush_run(self, rs: _RunState) -> None:
        for handle in rs.handles.values():
            try:
                handle.flush()
            except OSError:
                pass
        rs.last_flush_mono = self._mono()

    def _writer_periodic_flush(self) -> None:
        now = self._mono()
        for rs in list(self._w_open_runs.values()):
            if now - rs.last_flush_mono >= self.cfg.flush_interval_s:
                self._flush_run(rs)

    def _compute_comm_durations(self, rs: _RunState, end_mono: float) -> Dict[str, float]:
        durations = {"CONNECTED": 0.0, "PARTITIONED": 0.0, "DISCONNECTED": 0.0}
        log = rs.comm_log
        if not log:
            return durations
        for i, (ts, state) in enumerate(log):
            nxt = log[i + 1][0] if i + 1 < len(log) else end_mono
            if state in durations:
                durations[state] += max(0.0, nxt - ts)
        return {k: round(v, 2) for k, v in durations.items()}

    def _w_finalize(self, job: dict) -> None:
        run_id = job.get("run_id")
        rs = self._w_open_runs.get(run_id)
        finalize_mono = job.get("finalize_mono") or self._mono()
        if rs is None:
            # Nothing open for this run (e.g. INTERRUPTED reconciliation of a
            # run from a run directory this process never opened). Still
            # attempt a best-effort summary/pointer update if the directory
            # exists, so the bundle honestly reflects INTERRUPTED.
            run_dir = os.path.join(self.cfg.experiment_record_directory, run_id)
            if os.path.isdir(run_dir):
                self._write_terminal_summary(run_dir, {}, job, {}, None)
                self._write_checksums(run_dir)
                self._write_pointer(run_id, "FINALIZED", {}, {})
            with self._lock:
                self._finalizing = False
                self._last_finalized_run_id = run_id
            return
        try:
            duration_s = round(finalize_mono - rs.start_mono, 2)
            comm_durations = self._compute_comm_durations(rs, finalize_mono)
            self._write_terminal_summary(rs.dir, rs.experiment_meta, job, rs.__dict__,
                                         duration_s, comm_durations)
            self._flush_run(rs)
            for handle in rs.handles.values():
                try:
                    handle.close()
                except OSError:
                    pass
            self._write_checksums(rs.dir)
            self._write_pointer(run_id, "FINALIZED", rs.experiment_meta, {})
        except OSError as e:
            self._writer_degrade(run_id, "FINALIZE_WRITE_FAILED", str(e))
        finally:
            self._w_open_runs.pop(run_id, None)
            with self._lock:
                self._finalizing = False
                self._last_finalized_run_id = run_id

    def _write_terminal_summary(self, run_dir: str, experiment_meta: dict, job: dict,
                                rs_dict: dict, duration_s: Optional[float],
                                comm_durations: Optional[dict] = None) -> None:
        counters = rs_dict.get("counters") or {}
        last_tel = rs_dict.get("last_telemetry") or {}
        original = rs_dict.get("original_mission_meta") or {}
        # Authoritative terminal evidence (task section 2) -- whatever the
        # run's own controller(s) proved via a named terminal event (e.g.
        # STOP_COMPLETE). Falls back to the last periodic telemetry sample
        # ONLY for a field no terminal event ever proved (e.g. a COMPLETED_
        # HOLD/FAILED/SUSPENDED run with no dedicated terminal-evidence
        # event yet) -- never the reverse, so a proven terminal fact can
        # never be shadowed by a stale periodic sample.
        terminal = rs_dict.get("terminal_snapshot") or {}
        timing_marks = rs_dict.get("timing_marks") or {}

        def _terminal_or_last(field: str, last_key: str):
            val = terminal.get(field)
            return val if val is not None else last_tel.get(last_key)

        def _duration(mark_a: str, mark_b: str) -> Optional[float]:
            a, b = timing_marks.get(mark_a), timing_marks.get(mark_b)
            if a is None or b is None:
                return None
            return round(b - a, 3)

        comm_durations = comm_durations or {"CONNECTED": None, "PARTITIONED": None, "DISCONNECTED": None}
        summary = {
            "run": {
                "run_id": job.get("run_id"), "experiment_id": experiment_meta.get("experiment_id"),
                "experiment_type": experiment_meta.get("experiment_type"),
                "trial_number": experiment_meta.get("trial_number"),
                "result": job.get("outcome"), "terminal_reason": job.get("reason"),
                "duration_s": duration_s,
            },
            "mission": {
                "mission_id": original.get("mission_id"),
                "original_route_hash": original.get("original_route_hash"),
                "original_waypoint_count": original.get("original_waypoint_count"),
                # P0-3 evidence fix: revision_count (below) counts revised_
                # mission_rN.json records, and replan_controller._finalize_
                # revision() writes one on EVERY transaction terminal -- INCLUDING
                # a HOLD-only transaction (decision_policy requested a safety
                # hold; replan_controller._direct_safe_hold() never attempted
                # PLANNING/VALIDATING/UPLOAD) -- so revision_count alone cannot
                # tell "a replan was attempted" apart from "a HOLD consumed a
                # generation number". replan_attempt_count (below, under
                # "agent") increments only when the replan FSM actually enters
                # PLANNING (_w_index_event), which a HOLD-only transaction never
                # does -- it is the authoritative "an actual planning/upload
                # event occurred" signal. Do not revert to revision_count.
                "replanned": bool(counters.get("replan_attempt_count")),
                "revision_count": counters.get("revision_count", 0),
                "final_route_hash": terminal.get("route_hash"),
            },
            "vehicle": {
                "final_mode": _terminal_or_last("final_mode", "mode"),
                "final_armed": _terminal_or_last("final_armed", "armed"),
                "final_authority": _terminal_or_last("final_authority", "control_authority"),
            },
            # Explicit terminal mission-execution state/position evidence
            # (task section 2) -- None for any field no terminal event
            # proved; never derived from periodic telemetry.
            "final_state": {
                "mission_execution_state": terminal.get("mission_execution_state"),
                "mission_execution_phase": terminal.get("mission_execution_phase"),
                "current_waypoint": terminal.get("current_waypoint"),
                "mission_count": terminal.get("mission_count"),
                "mission_id": terminal.get("mission_id"),
                "route_hash": terminal.get("route_hash"),
            },
            # Stop-specific terminal evidence (task section 2), present only
            # when this run ended via a proven Stop transaction.
            "stop": terminal.get("stop_evidence"),
            "agent": {
                "decision_snapshot_count": counters.get("decision_snapshot_count", 0),
                # decision_change_count (E2 water-trial integration task
                # section 11): counts transitions of decision_engine.py's
                # rule-based battery/GPS/mission-phase label ONLY -- kept for
                # backward compatibility. It does NOT reflect risk_model.py's
                # level/recommendation or replan_controller.py's own energy
                # decision; see the three fields below for those.
                "decision_change_count": counters.get("decision_change_count", 0),
                "risk_level_change_count": counters.get("risk_level_change_count", 0),
                "recommendation_change_count": counters.get("recommendation_change_count", 0),
                "replan_decision_change_count": counters.get("replan_decision_change_count", 0),
                "replan_attempt_count": counters.get("replan_attempt_count", 0),
            },
            "communication": {
                "connected_duration_s": comm_durations.get("CONNECTED"),
                "partitioned_duration_s": comm_durations.get("PARTITIONED"),
                "disconnected_duration_s": comm_durations.get("DISCONNECTED"),
                "disconnect_count": counters.get("disconnect_count", 0),
                "reconnect_count": counters.get("reconnect_count", 0),
            },
            "safety": {
                "safe_hold_count": counters.get("safe_hold_count", 0),
                "validation_failure_count": counters.get("validation_failure_count", 0),
                "fallback_count": counters.get("fallback_count", 0),
                "unsafe_auto_resume_detected": False,
            },
            "timing": {
                "start_to_running_s": _duration("start_requested", "first_running"),
                "first_trigger_to_hold_s": _duration("first_replan_trigger", "first_replan_hold_confirmed"),
                "first_trigger_to_revised_auto_s": _duration("first_replan_trigger", "first_replan_revised_auto"),
                "first_trigger_to_terminal_s": _duration("first_replan_trigger", "first_replan_terminal"),
                # E2 water-trial integration task section 11 -- absolute
                # (run-relative elapsed_s) first-occurrence marks for the
                # three-layer decision model, alongside the existing
                # first_replan_trigger-derived durations above. None if the
                # condition never occurred this run.
                "first_risk_escalation_at": timing_marks.get("first_risk_escalation_at"),
                "first_return_recommendation_at": timing_marks.get("first_return_recommendation_at"),
                "first_return_action_request_at": timing_marks.get("first_return_action_request_at"),
                "first_replan_transition_at": timing_marks.get("first_replan_transition_at"),
            },
            "recorder": {
                "degraded": self._degraded,
                "dropped_telemetry_records": self._dropped_telemetry,
                "dropped_event_records": self._dropped_event,
                "errors": (rs_dict.get("errors") or [])[-10:],
            },
            "experiment_metrics": {},
        }
        _write_json_atomic(os.path.join(run_dir, "summary.json"), summary)

    def _write_checksums(self, run_dir: str) -> None:
        lines = []
        for fname in sorted(os.listdir(run_dir)) if os.path.isdir(run_dir) else []:
            if fname not in _CHECKSUM_CANDIDATES and not fname.startswith("revised_mission_r"):
                continue
            path = os.path.join(run_dir, fname)
            if not os.path.isfile(path):
                continue
            h = hashlib.sha256()
            try:
                with open(path, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        h.update(chunk)
            except OSError:
                continue
            lines.append(f"{h.hexdigest()}  {fname}")
        try:
            with open(os.path.join(run_dir, "checksums.sha256"), "w") as f:
                f.write("\n".join(lines) + ("\n" if lines else ""))
        except OSError:
            pass

    def _write_pointer(self, run_id: str, state: str, experiment_meta: dict, run_meta: dict) -> None:
        try:
            os.makedirs(self.cfg.experiment_record_directory, exist_ok=True)
            pointer = {
                "run_id": run_id, "state": state,
                "experiment_meta": experiment_meta,
                "started_at_wall": run_meta.get("_start_mono") and self._clock(),
                "started_at_wall_iso": run_meta.get("start_time_utc"),
            }
            tmp = f"{self._pointer_path()}.tmp"
            with open(tmp, "w") as f:
                json.dump(pointer, f)
            os.replace(tmp, self._pointer_path())
        except OSError:
            pass
