#!/usr/bin/env python3
"""
OFFLINE post-run energy-calibration analysis for one Experiment Recorder run.

    python3 analyze_energy_run.py experiment_runs/<run-id>
    python3 analyze_energy_run.py experiment_runs/<run-id> --output /path/to/out.json
    python3 analyze_energy_run.py experiment_runs/<run-id> --gap-threshold-s 8.0

This tool is READ-ONLY and OFFLINE by design (energy-calibration recorder
task, Phase 3):
  * it never modifies any file inside the run directory (telemetry.csv,
    manifest.json, checksums.sha256, ... are opened for reading only);
  * it never contacts the vehicle, the Operator backend, or any network
    service -- everything it reports comes from the recorded evidence
    already on disk;
  * it never writes calibrated parameters back into any config file --
    calibration (choosing conservative_current_A from repeated trials) is a
    human decision made from this tool's OUTPUT, not something this tool
    does automatically.

It computes measured Ah/Wh via trapezoidal integration of the raw
current_A/voltage_V columns telemetry.csv already records, GPS path
distance from successive lat/lon samples, and a simple model-vs-measured
comparison using the SAME conservative_current_A / design_speed_mps /
nominal_capacity_Ah / usable_capacity_factor / mission_reserve_fraction /
rtl_reserve_fraction parameters that were actually active during the run
(config_snapshot.json). This is for CALIBRATION ANALYSIS ONLY -- see the
module docstring of replan_config.py / mission_feasibility.py for the
online model this tool never touches.

Output: a readable console report, plus a structured
energy_analysis_<run_id>.json written OUTSIDE the run directory by default
(alongside it, i.e. as a sibling file in the same experiment_runs/
directory) so the run bundle's own checksums.sha256 is never invalidated.
Use --output to redirect it elsewhere.
"""
import argparse
import csv
import json
import os
import statistics
import sys
from typing import Any, Dict, List, Optional, Tuple

import geo

# ── Defaults (explicit, documented, overridable -- never silently assumed) ──
# Applies to BOTH the power-telemetry (current/voltage) integration and the
# GPS-distance reconstruction: a gap this large or larger between two
# consecutive samples is skipped and reported rather than bridged as if it
# were continuous evidence. 8x the default 2 Hz sample interval (0.5s) is a
# reasonable "something stalled" floor; it is trivially overridden per run
# via --gap-threshold-s if a run used a different experiment_record_
# telemetry_hz (this tool reads the run's OWN configured hz from
# config_snapshot.json to pick a better default when --gap-threshold-s is
# not given -- see _default_gap_threshold_s below).
_FALLBACK_GAP_THRESHOLD_S = 5.0
_GAP_THRESHOLD_HZ_MULTIPLE = 8.0

# "Moving" criterion for the moving/AUTO-interval metrics below. There is no
# existing online moving-threshold to reuse (task: do not invent an arbitrary
# moving threshold unless clearly documented and exposed as a named
# parameter) -- 0.1 m/s is comfortably above typical stationary-GPS
# groundspeed jitter and comfortably below Scout's design_speed_mps
# (field-calibrated prototype default -- see replan_config.py), and is
# always echoed in the output under "analysis_parameters" so it is never a
# hidden assumption.
_DEFAULT_MOVING_SPEED_THRESHOLD_MPS = 0.1


# ══════════════════════════════════════════════════════════════════════════
# Small, dependency-free helpers
# ══════════════════════════════════════════════════════════════════════════
def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, float):
        return v
    s = str(v).strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_bool(v: Any) -> Optional[bool]:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s == "":
        return None
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return None


def _read_json(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _read_csv_rows(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    with open(path, "r", newline="") as f:
        return list(csv.DictReader(f))


def _round(v: Optional[float], n: int = 6) -> Optional[float]:
    return None if v is None else round(v, n)


def _median(values: List[float]) -> Optional[float]:
    return None if not values else round(statistics.median(values), 6)


def _mean(values: List[float]) -> Optional[float]:
    return None if not values else round(sum(values) / len(values), 6)


def _percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 6)
    k = (len(ordered) - 1) * (pct / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    if lo == hi:
        return round(ordered[lo], 6)
    frac = k - lo
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * frac, 6)


# ══════════════════════════════════════════════════════════════════════════
# Row parsing -- one pass, typed, never raises on a malformed row
# ══════════════════════════════════════════════════════════════════════════
class Row:
    __slots__ = (
        "raw", "t_mono", "elapsed_s", "timestamp_utc", "latitude", "longitude",
        "position_age_s", "speed_m_s", "mode", "armed",
        "physical_battery_percent", "battery_valid", "battery_source",
        "injected_battery_percent", "voltage_V", "current_A",
        "communication_state", "mission_execution_state", "control_authority",
    )

    def __init__(self, raw: dict):
        self.raw = raw
        self.t_mono = _to_float(raw.get("monotonic_time_s"))
        self.elapsed_s = _to_float(raw.get("elapsed_s"))
        self.timestamp_utc = raw.get("timestamp_utc") or None
        self.latitude = _to_float(raw.get("latitude"))
        self.longitude = _to_float(raw.get("longitude"))
        self.position_age_s = _to_float(raw.get("position_age_s"))
        self.speed_m_s = _to_float(raw.get("speed_m_s"))
        self.mode = (raw.get("mode") or None)
        self.armed = _to_bool(raw.get("armed"))
        self.physical_battery_percent = _to_float(raw.get("physical_battery_percent"))
        self.battery_valid = _to_bool(raw.get("battery_valid"))
        self.battery_source = raw.get("battery_source") or None
        self.injected_battery_percent = _to_float(raw.get("injected_battery_percent"))
        self.voltage_V = _to_float(raw.get("voltage_V"))
        self.current_A = _to_float(raw.get("current_A"))
        self.communication_state = raw.get("communication_state") or None
        self.mission_execution_state = raw.get("mission_execution_state") or None
        self.control_authority = raw.get("control_authority") or None

    @property
    def has_position(self) -> bool:
        return self.latitude is not None and self.longitude is not None


def _parse_rows(raw_rows: List[dict]) -> List[Row]:
    return [Row(r) for r in raw_rows]


# ══════════════════════════════════════════════════════════════════════════
# Generic trapezoidal accumulator over consecutive samples
# ══════════════════════════════════════════════════════════════════════════
class _Accumulator:
    """Walks consecutive Row pairs once, accumulating a trapezoidal integral
    of `value_fn` over time (monotonic seconds) plus the total valid time
    covered, while honouring `include_fn` (both endpoints must pass) and the
    gap threshold. Never bridges a skipped/invalid interval -- gaps and
    invalid intervals are counted, never silently folded into the integral.
    """

    def __init__(self, gap_threshold_s: float):
        self.gap_threshold_s = gap_threshold_s
        self.integral = 0.0
        self.valid_time_s = 0.0
        self.segments_used = 0
        self.segments_skipped_invalid_value = 0
        self.segments_skipped_negative_dt = 0
        self.segments_skipped_gap = 0
        self.gaps: List[Dict[str, Any]] = []  # each: {start_elapsed_s, dt_s}

    def add(self, r1: Row, r2: Row, value_fn, include_fn=None) -> None:
        if r1.t_mono is None or r2.t_mono is None:
            return
        dt = r2.t_mono - r1.t_mono
        if dt <= 0:
            self.segments_skipped_negative_dt += 1
            return
        if dt >= self.gap_threshold_s:
            self.segments_skipped_gap += 1
            self.gaps.append({
                "start_elapsed_s": r1.elapsed_s, "end_elapsed_s": r2.elapsed_s,
                "dt_s": round(dt, 3),
            })
            return
        if include_fn is not None and not (include_fn(r1) and include_fn(r2)):
            return
        v1, v2 = value_fn(r1), value_fn(r2)
        if v1 is None or v2 is None:
            self.segments_skipped_invalid_value += 1
            return
        self.integral += ((v1 + v2) / 2.0) * dt
        self.valid_time_s += dt
        self.segments_used += 1

    def mean(self) -> Optional[float]:
        return None if self.valid_time_s <= 0 else self.integral / self.valid_time_s


def _walk_gaps_and_intervals(rows: List[Row], gap_threshold_s: float) -> Dict[str, Any]:
    """Timestamp-quality pass (task: DATA QUALITY section), independent of
    which value column is being integrated -- based purely on
    monotonic_time_s deltas across ALL consecutive samples."""
    dts = []
    negative_or_zero = 0
    largest_gap_s = 0.0
    for r1, r2 in zip(rows, rows[1:]):
        if r1.t_mono is None or r2.t_mono is None:
            continue
        dt = r2.t_mono - r1.t_mono
        if dt <= 0:
            negative_or_zero += 1
            continue
        dts.append(dt)
        if dt > largest_gap_s:
            largest_gap_s = dt
    return {
        "median_interval_s": _median(dts),
        "largest_gap_s": round(largest_gap_s, 3) if dts else None,
        "negative_or_zero_interval_count": negative_or_zero,
        "interval_sample_count": len(dts),
    }


# ══════════════════════════════════════════════════════════════════════════
# Distance / motion
# ══════════════════════════════════════════════════════════════════════════
def _gps_distance_m(rows: List[Row], gap_threshold_s: float) -> Dict[str, Any]:
    total_m = 0.0
    used_pairs = 0
    skipped_gap = 0
    skipped_invalid = 0
    for r1, r2 in zip(rows, rows[1:]):
        if r1.t_mono is None or r2.t_mono is None:
            continue
        dt = r2.t_mono - r1.t_mono
        if dt <= 0:
            continue
        if dt >= gap_threshold_s:
            skipped_gap += 1
            continue
        if not (r1.has_position and r2.has_position):
            skipped_invalid += 1
            continue
        total_m += geo.haversine_m(r1.latitude, r1.longitude, r2.latitude, r2.longitude)
        used_pairs += 1
    return {
        "distance_m": round(total_m, 2),
        "position_pairs_used": used_pairs,
        "position_pairs_skipped_gap": skipped_gap,
        "position_pairs_skipped_invalid": skipped_invalid,
    }


def _speed_stats(rows: List[Row]) -> Dict[str, Any]:
    speeds = [r.speed_m_s for r in rows if r.speed_m_s is not None and r.speed_m_s >= 0]
    return {
        "valid_sample_count": len(speeds),
        "mean_mps": _mean(speeds),
        "median_mps": _median(speeds),
        "p85_mps": _percentile(speeds, 85.0),
        "p95_mps": _percentile(speeds, 95.0),
        "max_mps": (None if not speeds else round(max(speeds), 6)),
    }


def _moving_duration_s(rows: List[Row], threshold_mps: float, gap_threshold_s: float) -> float:
    total = 0.0
    for r1, r2 in zip(rows, rows[1:]):
        if r1.t_mono is None or r2.t_mono is None:
            continue
        dt = r2.t_mono - r1.t_mono
        if dt <= 0 or dt >= gap_threshold_s:
            continue
        if r1.speed_m_s is None or r2.speed_m_s is None:
            continue
        if r1.speed_m_s > threshold_mps and r2.speed_m_s > threshold_mps:
            total += dt
    return round(total, 2)


# ══════════════════════════════════════════════════════════════════════════
# Main analysis
# ══════════════════════════════════════════════════════════════════════════
def _default_gap_threshold_s(config_snapshot: Optional[dict]) -> float:
    hz = None
    if config_snapshot:
        hz = ((config_snapshot.get("recorder") or {}).get("telemetry_hz"))
    if not hz or hz <= 0:
        return _FALLBACK_GAP_THRESHOLD_S
    return max(_FALLBACK_GAP_THRESHOLD_S, (1.0 / hz) * _GAP_THRESHOLD_HZ_MULTIPLE)


def analyze(run_dir: str, gap_threshold_s: Optional[float] = None,
            moving_speed_threshold_mps: float = _DEFAULT_MOVING_SPEED_THRESHOLD_MPS) -> Dict[str, Any]:
    run_dir = os.path.normpath(run_dir)
    run_id = os.path.basename(run_dir)
    manifest = _read_json(os.path.join(run_dir, "manifest.json")) or {}
    config_snapshot = _read_json(os.path.join(run_dir, "config_snapshot.json")) or {}
    summary = _read_json(os.path.join(run_dir, "summary.json"))  # optional -- run may be unfinalized
    raw_rows = _read_csv_rows(os.path.join(run_dir, "telemetry.csv"))
    if not raw_rows:
        raise SystemExit(
            f"No telemetry.csv rows found in {run_dir!r} -- nothing to analyze. "
            "(Is this a valid, started run directory?)"
        )
    rows = _parse_rows(raw_rows)
    rows.sort(key=lambda r: (r.t_mono if r.t_mono is not None else float("inf")))

    if gap_threshold_s is None:
        gap_threshold_s = _default_gap_threshold_s(config_snapshot)

    experiment_meta = manifest.get("experiment") or {}
    run_meta = manifest.get("run") or {}

    # ── RUN ──────────────────────────────────────────────────────────────
    first_ts = next((r.timestamp_utc for r in rows if r.timestamp_utc), None)
    last_ts = next((r.timestamp_utc for r in reversed(rows) if r.timestamp_utc), None)
    first_mono = next((r.t_mono for r in rows if r.t_mono is not None), None)
    last_mono = next((r.t_mono for r in reversed(rows) if r.t_mono is not None), None)
    duration_s = (None if first_mono is None or last_mono is None
                  else round(last_mono - first_mono, 3))

    run_block = {
        "run_id": run_meta.get("run_id") or run_id,
        "experiment_id": experiment_meta.get("experiment_id"),
        "experiment_type": experiment_meta.get("experiment_type"),
        "trial_number": experiment_meta.get("trial_number"),
        "scenario": experiment_meta.get("scenario"),
        "vehicle_id": run_meta.get("vehicle_id"),
        "start_utc": first_ts, "end_utc": last_ts,
        "duration_s": duration_s,
        "recorder_result": (summary or {}).get("run", {}).get("result"),
        "finalized": summary is not None,
    }

    # ── DATA QUALITY ─────────────────────────────────────────────────────
    interval_stats = _walk_gaps_and_intervals(rows, gap_threshold_s)
    valid_current = [r for r in rows if r.current_A is not None]
    valid_voltage = [r for r in rows if r.voltage_V is not None]
    valid_position = [r for r in rows if r.has_position]
    valid_speed = [r for r in rows if r.speed_m_s is not None]
    synthetic_battery_present = any(r.injected_battery_percent is not None for r in rows)

    # ── ENERGY (Ah / Wh via trapezoidal integration -- POWER TELEMETRY
    #    REQUIREMENTS: current_A/voltage_V are ALWAYS the physical raw
    #    measurement, regardless of any battery_percent injection) ─────────
    ah_acc = _Accumulator(gap_threshold_s)
    wh_acc = _Accumulator(gap_threshold_s)
    armed_current_acc = _Accumulator(gap_threshold_s)
    auto_current_acc = _Accumulator(gap_threshold_s)
    moving_current_acc = _Accumulator(gap_threshold_s)
    moving_wh_acc = _Accumulator(gap_threshold_s)

    def _is_armed(r: Row) -> bool:
        return r.armed is True

    def _is_auto(r: Row) -> bool:
        return (r.mode or "").upper() == "AUTO"

    def _is_moving(r: Row) -> bool:
        return r.speed_m_s is not None and r.speed_m_s > moving_speed_threshold_mps

    for r1, r2 in zip(rows, rows[1:]):
        ah_acc.add(r1, r2, lambda r: r.current_A)
        wh_acc.add(r1, r2, lambda r: (None if r.voltage_V is None or r.current_A is None
                                      else r.voltage_V * r.current_A))
        armed_current_acc.add(r1, r2, lambda r: r.current_A, include_fn=_is_armed)
        auto_current_acc.add(r1, r2, lambda r: r.current_A, include_fn=_is_auto)
        moving_current_acc.add(r1, r2, lambda r: r.current_A, include_fn=_is_moving)
        moving_wh_acc.add(r1, r2, lambda r: (None if r.voltage_V is None or r.current_A is None
                                             else r.voltage_V * r.current_A), include_fn=_is_moving)

    # None (not 0.0) when zero valid segments were integrated -- "no power
    # telemetry was usable this run" must never read the same as "measured
    # zero current/energy" (task: do not fabricate null data as zero).
    integrated_Ah = None if ah_acc.segments_used == 0 else ah_acc.integral / 3600.0
    integrated_Wh = None if wh_acc.segments_used == 0 else wh_acc.integral / 3600.0
    moving_Wh = None if moving_wh_acc.segments_used == 0 else moving_wh_acc.integral / 3600.0

    usable_power_time_s = ah_acc.valid_time_s
    power_coverage_percent = (
        None if not duration_s or duration_s <= 0
        else round(100.0 * usable_power_time_s / duration_s, 2)
    )

    data_quality = {
        "telemetry_sample_count": len(rows),
        "valid_current_sample_count": len(valid_current),
        "valid_voltage_sample_count": len(valid_voltage),
        "valid_position_sample_count": len(valid_position),
        "valid_speed_sample_count": len(valid_speed),
        "largest_telemetry_gap_s": interval_stats["largest_gap_s"],
        "median_telemetry_interval_s": interval_stats["median_interval_s"],
        "negative_or_zero_interval_count": interval_stats["negative_or_zero_interval_count"],
        "power_telemetry_coverage_percent_of_duration": power_coverage_percent,
        "gap_threshold_s_used": gap_threshold_s,
        "gaps_skipped_from_energy_integration": ah_acc.gaps,
        "synthetic_battery_injection_present": synthetic_battery_present,
    }

    # ── BATTERY ──────────────────────────────────────────────────────────
    phys_rows = [r for r in rows if r.physical_battery_percent is not None]
    start_battery_pct = phys_rows[0].physical_battery_percent if phys_rows else None
    end_battery_pct = phys_rows[-1].physical_battery_percent if phys_rows else None
    soc_drop_points = (
        None if start_battery_pct is None or end_battery_pct is None
        else round(start_battery_pct - end_battery_pct, 3)
    )
    voltages = [r.voltage_V for r in rows if r.voltage_V is not None]
    start_voltage = valid_voltage[0].voltage_V if valid_voltage else None
    end_voltage = valid_voltage[-1].voltage_V if valid_voltage else None

    battery_block = {
        "start_physical_battery_percent": start_battery_pct,
        "end_physical_battery_percent": end_battery_pct,
        "observed_soc_drop_points": soc_drop_points,
        "start_voltage_V": start_voltage,
        "end_voltage_V": end_voltage,
        "min_voltage_V": (None if not voltages else round(min(voltages), 3)),
        "mean_voltage_V": _mean(voltages),
    }

    # ── CURRENT ──────────────────────────────────────────────────────────
    currents = [r.current_A for r in rows if r.current_A is not None]
    current_block = {
        "mean_current_A": _mean(currents),
        "median_current_A": _median(currents),
        "max_current_A": (None if not currents else round(max(currents), 4)),
        "time_weighted_mean_current_A_full_run": _round(ah_acc.mean(), 4),
        "time_weighted_mean_current_A_armed": _round(armed_current_acc.mean(), 4),
        "time_weighted_mean_current_A_auto_mode": _round(auto_current_acc.mean(), 4),
        "time_weighted_mean_current_A_moving": _round(moving_current_acc.mean(), 4),
    }

    # ── ENERGY block ─────────────────────────────────────────────────────
    energy_block = {
        "integrated_Ah": _round(integrated_Ah, 4),
        "integrated_Wh": _round(integrated_Wh, 4),
        "integration_method": "trapezoidal over consecutive samples with valid monotonic_time_s "
                               "deltas below gap_threshold_s; gaps/invalid intervals skipped, not bridged",
        "moving_only_integrated_Wh": _round(moving_Wh, 4),
    }

    # ── MOTION ───────────────────────────────────────────────────────────
    dist = _gps_distance_m(rows, gap_threshold_s)
    speed_stats = _speed_stats(rows)
    moving_duration_s = _moving_duration_s(rows, moving_speed_threshold_mps, gap_threshold_s)
    motion_block = {
        "distance_m": dist["distance_m"],
        "distance_km": round(dist["distance_m"] / 1000.0, 4),
        "position_pairs_used": dist["position_pairs_used"],
        "position_pairs_skipped_gap": dist["position_pairs_skipped_gap"],
        "position_pairs_skipped_invalid": dist["position_pairs_skipped_invalid"],
        "speed": speed_stats,
        "moving_duration_s": moving_duration_s,
        "moving_speed_threshold_mps": moving_speed_threshold_mps,
    }

    # ── DISTANCE NORMALIZATION ──────────────────────────────────────────
    distance_km = motion_block["distance_km"]
    normalization_block = {
        "Ah_per_km": (None if not distance_km or integrated_Ah is None
                      else round(integrated_Ah / distance_km, 4)),
        "Wh_per_km": (None if not distance_km or integrated_Wh is None
                      else round(integrated_Wh / distance_km, 4)),
    }

    # ── MODEL COMPARISON ─────────────────────────────────────────────────
    replan_values = ((config_snapshot.get("replan") or {}).get("values")) or {}
    model_params = {
        "conservative_current_A": replan_values.get("conservative_current_A"),
        "design_speed_mps": replan_values.get("design_speed_mps"),
        "nominal_capacity_Ah": replan_values.get("nominal_capacity_Ah"),
        "usable_capacity_factor": replan_values.get("usable_capacity_factor"),
        "mission_reserve_fraction": replan_values.get("mission_reserve_fraction"),
        "rtl_reserve_fraction": replan_values.get("rtl_reserve_fraction"),
    }

    model_comparison: Dict[str, Any] = dict(model_params)
    conservative_current_A = model_params["conservative_current_A"]
    design_speed_mps = model_params["design_speed_mps"]
    predicted_duration_h = None
    predicted_consumption_Ah = None
    equivalent_average_current_A = None
    if design_speed_mps and design_speed_mps > 0 and dist["distance_m"] > 0:
        predicted_duration_h = dist["distance_m"] / design_speed_mps / 3600.0
        if conservative_current_A is not None:
            predicted_consumption_Ah = conservative_current_A * predicted_duration_h
        if predicted_duration_h > 0 and integrated_Ah is not None:
            equivalent_average_current_A = integrated_Ah / predicted_duration_h

    signed_error_Ah = None
    signed_error_percent = None
    conservative_ratio = None
    if predicted_consumption_Ah is not None and integrated_Ah is not None:
        signed_error_Ah = predicted_consumption_Ah - integrated_Ah
        if integrated_Ah:
            signed_error_percent = round(100.0 * signed_error_Ah / integrated_Ah, 2)
            conservative_ratio = round(predicted_consumption_Ah / integrated_Ah, 4)
        signed_error_Ah = round(signed_error_Ah, 4)

    model_comparison.update({
        "travelled_distance_m": dist["distance_m"],
        "predicted_duration_h": _round(predicted_duration_h, 4),
        "predicted_consumption_Ah": _round(predicted_consumption_Ah, 4),
        "measured_integrated_Ah": _round(integrated_Ah, 4),
        # signed_error_Ah = predicted - measured. POSITIVE means the online
        # model predicted MORE Ah than was actually measured -- i.e. a
        # conservative OVER-prediction (the model budgeted more energy than
        # the run actually used). NEGATIVE means the model UNDER-predicted
        # (predicted less than measured -- non-conservative, worth flagging).
        # signed_error_percent is the same sign, relative to measured Ah.
        "signed_error_Ah": signed_error_Ah,
        "signed_error_percent_of_measured": signed_error_percent,
        "signed_error_interpretation": (
            None if signed_error_Ah is None else
            ("predicted MORE than measured -- conservative over-prediction" if signed_error_Ah > 0
             else "predicted LESS than measured -- under-prediction, not conservative" if signed_error_Ah < 0
             else "predicted equals measured")
        ),
        "conservative_ratio_predicted_over_measured": conservative_ratio,
        # equivalent_average_current_A: measured_integrated_Ah divided by the
        # SAME distance/design-speed-equivalent duration used for the
        # prediction above -- directly comparable to conservative_current_A
        # across repeated characterization runs. Distinct from (never
        # conflated with) time_weighted_mean_current_A_moving in the CURRENT
        # block above, which is the actual elapsed-moving-time average.
        "equivalent_average_current_A": _round(equivalent_average_current_A, 4),
        "time_weighted_measured_average_current_A_during_motion": current_block["time_weighted_mean_current_A_moving"],
    })

    # ── SOC comparison (SOC-estimator-derived, NOT ground truth) ─────────
    soc_estimate = None
    if (soc_drop_points is not None and model_params["nominal_capacity_Ah"] is not None
            and model_params["usable_capacity_factor"] is not None):
        soc_estimate = round(
            model_params["nominal_capacity_Ah"] * model_params["usable_capacity_factor"]
            * soc_drop_points / 100.0, 4,
        )
    soc_comparison = {
        "observed_soc_drop_points": soc_drop_points,
        "estimated_capacity_from_soc_drop_Ah": soc_estimate,
        "label": "SOC-ESTIMATOR-DERIVED -- NOT ground truth; the primary measured "
                 "quantities are integrated_Ah/integrated_Wh above.",
    }

    return {
        "schema_version": "energy-analysis-v1",
        "analysis_parameters": {
            "gap_threshold_s": gap_threshold_s,
            "moving_speed_threshold_mps": moving_speed_threshold_mps,
        },
        "run": run_block,
        "data_quality": data_quality,
        "battery": battery_block,
        "current": current_block,
        "energy": energy_block,
        "motion": motion_block,
        "distance_normalization": normalization_block,
        "model_comparison": model_comparison,
        "soc_comparison": soc_comparison,
    }


# ══════════════════════════════════════════════════════════════════════════
# Console report
# ══════════════════════════════════════════════════════════════════════════
def _fmt(v: Any) -> str:
    return "—" if v is None else str(v)


def print_report(result: Dict[str, Any]) -> None:
    r, dq, bat, cur, en, mo, norm, mc, soc = (
        result["run"], result["data_quality"], result["battery"], result["current"],
        result["energy"], result["motion"], result["distance_normalization"],
        result["model_comparison"], result["soc_comparison"],
    )
    print("=" * 72)
    print(f"ENERGY ANALYSIS -- {r['run_id']}")
    print("=" * 72)
    print(f"experiment_id={_fmt(r['experiment_id'])} experiment_type={_fmt(r['experiment_type'])} "
          f"scenario={_fmt(r['scenario'])} trial={_fmt(r['trial_number'])}")
    print(f"vehicle_id={_fmt(r['vehicle_id'])} start={_fmt(r['start_utc'])} end={_fmt(r['end_utc'])}")
    print(f"duration_s={_fmt(r['duration_s'])} finalized={r['finalized']} result={_fmt(r['recorder_result'])}")
    print()
    print("-- DATA QUALITY --")
    print(f"  samples={dq['telemetry_sample_count']} valid_current={dq['valid_current_sample_count']} "
          f"valid_voltage={dq['valid_voltage_sample_count']} valid_position={dq['valid_position_sample_count']} "
          f"valid_speed={dq['valid_speed_sample_count']}")
    print(f"  largest_gap_s={_fmt(dq['largest_telemetry_gap_s'])} "
          f"median_interval_s={_fmt(dq['median_telemetry_interval_s'])} "
          f"negative/zero_intervals_ignored={dq['negative_or_zero_interval_count']}")
    print(f"  power_telemetry_coverage={_fmt(dq['power_telemetry_coverage_percent_of_duration'])}% "
          f"gap_threshold_s={dq['gap_threshold_s_used']} "
          f"gaps_skipped={len(dq['gaps_skipped_from_energy_integration'])}")
    print(f"  synthetic_battery_injection_present={dq['synthetic_battery_injection_present']}")
    print()
    print("-- BATTERY --")
    print(f"  start%={_fmt(bat['start_physical_battery_percent'])} end%={_fmt(bat['end_physical_battery_percent'])} "
          f"SOC_drop_points={_fmt(bat['observed_soc_drop_points'])}")
    print(f"  start_V={_fmt(bat['start_voltage_V'])} end_V={_fmt(bat['end_voltage_V'])} "
          f"min_V={_fmt(bat['min_voltage_V'])} mean_V={_fmt(bat['mean_voltage_V'])}")
    print()
    print("-- CURRENT --")
    print(f"  mean_A={_fmt(cur['mean_current_A'])} median_A={_fmt(cur['median_current_A'])} "
          f"max_A={_fmt(cur['max_current_A'])}")
    print(f"  time_weighted_mean_A: full_run={_fmt(cur['time_weighted_mean_current_A_full_run'])} "
          f"armed={_fmt(cur['time_weighted_mean_current_A_armed'])} "
          f"auto_mode={_fmt(cur['time_weighted_mean_current_A_auto_mode'])} "
          f"moving={_fmt(cur['time_weighted_mean_current_A_moving'])}")
    print()
    print("-- ENERGY --")
    print(f"  integrated_Ah={_fmt(en['integrated_Ah'])} integrated_Wh={_fmt(en['integrated_Wh'])} "
          f"moving_only_Wh={_fmt(en['moving_only_integrated_Wh'])}")
    print()
    print("-- MOTION --")
    print(f"  distance_m={_fmt(mo['distance_m'])} ({_fmt(mo['distance_km'])} km) "
          f"pairs_used={mo['position_pairs_used']} skipped_gap={mo['position_pairs_skipped_gap']} "
          f"skipped_invalid={mo['position_pairs_skipped_invalid']}")
    sp = mo["speed"]
    print(f"  speed_mps: mean={_fmt(sp['mean_mps'])} median={_fmt(sp['median_mps'])} "
          f"p85={_fmt(sp['p85_mps'])} p95={_fmt(sp['p95_mps'])} max={_fmt(sp['max_mps'])}")
    print(f"  moving_duration_s={_fmt(mo['moving_duration_s'])} "
          f"(threshold={mo['moving_speed_threshold_mps']} m/s)")
    print()
    print("-- DISTANCE NORMALIZATION --")
    print(f"  Ah/km={_fmt(norm['Ah_per_km'])} Wh/km={_fmt(norm['Wh_per_km'])}")
    print()
    print("-- MODEL COMPARISON (calibration analysis only; never written back) --")
    print(f"  conservative_current_A={_fmt(mc['conservative_current_A'])} "
          f"design_speed_mps={_fmt(mc['design_speed_mps'])} "
          f"nominal_capacity_Ah={_fmt(mc['nominal_capacity_Ah'])} "
          f"usable_capacity_factor={_fmt(mc['usable_capacity_factor'])}")
    print(f"  mission_reserve_fraction={_fmt(mc['mission_reserve_fraction'])} "
          f"rtl_reserve_fraction={_fmt(mc['rtl_reserve_fraction'])}")
    print(f"  predicted_consumption_Ah={_fmt(mc['predicted_consumption_Ah'])} "
          f"measured_integrated_Ah={_fmt(mc['measured_integrated_Ah'])}")
    print(f"  signed_error_Ah={_fmt(mc['signed_error_Ah'])} "
          f"({_fmt(mc['signed_error_interpretation'])})")
    print(f"  signed_error_percent_of_measured={_fmt(mc['signed_error_percent_of_measured'])} "
          f"conservative_ratio={_fmt(mc['conservative_ratio_predicted_over_measured'])}")
    print(f"  equivalent_average_current_A={_fmt(mc['equivalent_average_current_A'])} "
          f"time_weighted_measured_avg_current_during_motion={_fmt(mc['time_weighted_measured_average_current_A_during_motion'])}")
    print()
    print("-- SOC COMPARISON (SOC-estimator-derived, NOT ground truth) --")
    print(f"  observed_soc_drop_points={_fmt(soc['observed_soc_drop_points'])} "
          f"estimated_capacity_from_soc_drop_Ah={_fmt(soc['estimated_capacity_from_soc_drop_Ah'])}")
    print("=" * 72)


def _default_output_path(run_dir: str) -> str:
    run_dir = os.path.normpath(run_dir)
    run_id = os.path.basename(run_dir)
    # Sibling to the run directory (e.g. inside experiment_runs/, next to
    # run-.../), NEVER inside it -- the run bundle's own checksums.sha256
    # must never be invalidated by this tool (task: OUTPUT section).
    parent = os.path.dirname(run_dir)
    return os.path.join(parent, f"energy_analysis_{run_id}.json")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline energy-calibration analysis for one Experiment Recorder run. "
                     "Read-only; never modifies the run directory; never contacts the vehicle.",
    )
    parser.add_argument("run_dir", help="Path to an experiment_runs/<run-id> directory")
    parser.add_argument("--output", default=None,
                        help="Path to write the derived energy_analysis_<run-id>.json "
                             "(default: alongside the run directory, never inside it)")
    parser.add_argument("--gap-threshold-s", type=float, default=None,
                        help="Telemetry gap (seconds) at/above which a sample-to-sample "
                             "interval is skipped rather than integrated/bridged "
                             "(default: derived from the run's own recorded telemetry_hz)")
    parser.add_argument("--moving-speed-threshold-mps", type=float,
                        default=_DEFAULT_MOVING_SPEED_THRESHOLD_MPS,
                        help=f"Groundspeed (m/s) above which a sample counts as 'moving' "
                             f"(default: {_DEFAULT_MOVING_SPEED_THRESHOLD_MPS})")
    parser.add_argument("--quiet", action="store_true", help="Suppress the console report; JSON only")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.run_dir):
        print(f"error: {args.run_dir!r} is not a directory", file=sys.stderr)
        return 2

    result = analyze(
        args.run_dir,
        gap_threshold_s=args.gap_threshold_s,
        moving_speed_threshold_mps=args.moving_speed_threshold_mps,
    )

    if not args.quiet:
        print_report(result)

    out_path = args.output or _default_output_path(args.run_dir)
    tmp_path = f"{out_path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(result, f, indent=2, sort_keys=True, default=str)
    os.replace(tmp_path, out_path)
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
