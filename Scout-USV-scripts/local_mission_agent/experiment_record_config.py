"""
Typed, overridable settings for the thesis Experiment Recorder
(experiment_recorder.py).

Same idiom as mission_execution_config.py / replan_config.py: one frozen
dataclass, safe defaults, EXPERIMENT_RECORD_* environment overrides so a
systemd unit or a one-off bench run can retune without editing source.

This module carries NO behaviour of its own -- it only resolves numbers. The
recorder itself decides what "enabled" / "disabled" actually mean.
"""
import os
from dataclasses import dataclass, asdict
from typing import Optional


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip()


_DEFAULT_DIRECTORY = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "experiment_runs"
)


@dataclass(frozen=True)
class ExperimentRecordConfig:
    # ── Master switch (task section 45: default ON for the experiment period) ──
    # Zero behavioural difference when False: every recorder public method is a
    # fast no-op, no queue, no writer thread, no directory ever created.
    experiment_recording_enabled: bool = True

    # ── Output location (task section 7) ────────────────────────────────────
    experiment_record_directory: str = _DEFAULT_DIRECTORY

    # ── Telemetry sample rate (task section 22/23) ──────────────────────────
    # 2 Hz default; 1 Hz is acceptable per the task -- configurable either way.
    experiment_record_telemetry_hz: float = 2.0

    # ── Bounded queues (task section 24) ─────────────────────────────────────
    # High-priority: terminal lifecycle, decision changes, trigger/injection,
    # replan transitions, validation outcomes, hashes, upload/readback
    # evidence, communication transitions, safety/failures, Stop, COMPLETED_HOLD.
    # Low-priority: repetitive telemetry samples -- dropped first under pressure.
    experiment_record_queue_capacity: int = 4096
    experiment_record_low_queue_capacity: int = 1024

    # How long the writer thread waits for the next queued record before
    # looping back to check the stop flag / do periodic maintenance (flush).
    writer_poll_interval_s: float = 0.5
    # How often the writer flushes/rotates open file handles (telemetry.csv)
    # even if nothing new has been written -- bounds data loss on a crash
    # without fsync-ing on every single record (task section 0: never fsync
    # in the mission/replan critical path -- this is on the writer thread only).
    flush_interval_s: float = 2.0

    def to_dict(self) -> dict:
        return asdict(self)


_FIELD_ENV = {
    "experiment_recording_enabled": ("EXPERIMENT_RECORDING_ENABLED", _env_bool),
    "experiment_record_directory": ("EXPERIMENT_RECORD_DIRECTORY", _env_str),
    "experiment_record_telemetry_hz": ("EXPERIMENT_RECORD_TELEMETRY_HZ", _env_float),
    "experiment_record_queue_capacity": ("EXPERIMENT_RECORD_QUEUE_CAPACITY", _env_int),
    "experiment_record_low_queue_capacity": ("EXPERIMENT_RECORD_LOW_QUEUE_CAPACITY", _env_int),
    "writer_poll_interval_s": ("EXPERIMENT_RECORD_WRITER_POLL_INTERVAL_S", _env_float),
    "flush_interval_s": ("EXPERIMENT_RECORD_FLUSH_INTERVAL_S", _env_float),
}


def load() -> ExperimentRecordConfig:
    defaults = ExperimentRecordConfig()
    kwargs = {}
    for field, (env_name, caster) in _FIELD_ENV.items():
        kwargs[field] = caster(env_name, getattr(defaults, field))
    return ExperimentRecordConfig(**kwargs)


def resolve() -> "tuple":
    """Merge defaults < environment into one config plus a {field: source} map,
    mirroring mission_execution_config.resolve() / replan_config.resolve()."""
    defaults = ExperimentRecordConfig()
    kwargs = {}
    sources = {}
    for field, (env_name, caster) in _FIELD_ENV.items():
        default_val = getattr(defaults, field)
        if os.environ.get(env_name) is not None:
            kwargs[field] = caster(env_name, default_val)
            sources[field] = "environment"
        else:
            kwargs[field] = default_val
            sources[field] = "default"
    return ExperimentRecordConfig(**kwargs), sources


DEFAULT = load()
