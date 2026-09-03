"""
Focused tests for analyze_energy_run.py -- the OFFLINE energy-calibration
analysis tool (energy-calibration recorder task, Phase 5).

    python3 test_analyze_energy_run.py

Builds small synthetic run directories by hand (manifest.json,
config_snapshot.json, telemetry.csv) rather than going through the real
recorder -- these tests are about the ANALYSIS tool's arithmetic and
read-only contract, not the recorder's writer thread (see
test_experiment_recorder.py for those).
"""
import csv
import hashlib
import json
import os
import shutil
import tempfile
import unittest

import analyze_energy_run as aer

_FIELDS = [
    "timestamp_utc", "monotonic_time_s", "elapsed_s",
    "vehicle_id", "latitude", "longitude", "position_age_s",
    "speed_m_s", "mode", "armed",
    "physical_battery_percent", "battery_valid", "battery_source",
    "injected_battery_percent", "voltage_V", "current_A",
]


def _write_run(base_dir, run_id, rows, replan_values=None, experiment_meta=None,
              telemetry_hz=2.0):
    run_dir = os.path.join(base_dir, run_id)
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "telemetry.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_FIELDS, extrasaction="ignore", restval="")
        w.writeheader()
        for row in rows:
            w.writerow(row)
    manifest = {
        "schema_version": "experiment-record-v1",
        "experiment": experiment_meta or {
            "experiment_id": "E-ENERGY-CAL", "experiment_type": "ENERGY_CALIBRATION",
            "scenario": "long_back_and_forth_fixed_speed", "trial_number": 1,
        },
        "run": {"run_id": run_id, "vehicle_id": "usv-test"},
        "source": "SCOUT_LOCAL_AGENT",
    }
    with open(os.path.join(run_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f)
    config_snapshot = {
        "recorder": {"telemetry_hz": telemetry_hz},
        "replan": {"values": replan_values or {
            "conservative_current_A": 9.0, "design_speed_mps": 1.0,
            "nominal_capacity_Ah": 40.0, "usable_capacity_factor": 0.8,
            "mission_reserve_fraction": 0.15, "rtl_reserve_fraction": 0.05,
        }},
    }
    with open(os.path.join(run_dir, "config_snapshot.json"), "w") as f:
        json.dump(config_snapshot, f)
    return run_dir


def _row(t_mono, lat=None, lon=None, speed=None, current=None, voltage=None,
        battery=None, armed="True", mode="AUTO", elapsed=None, injected=None,
        vehicle_id="usv-test"):
    return {
        "timestamp_utc": f"2026-08-20T00:00:{t_mono:06.3f}+00:00",
        "monotonic_time_s": t_mono,
        "elapsed_s": t_mono if elapsed is None else elapsed,
        "vehicle_id": vehicle_id,
        "latitude": "" if lat is None else lat,
        "longitude": "" if lon is None else lon,
        "position_age_s": 0.1,
        "speed_m_s": "" if speed is None else speed,
        "mode": mode,
        "armed": armed,
        "physical_battery_percent": "" if battery is None else battery,
        "battery_valid": "True" if battery is not None else "",
        "battery_source": "PHYSICAL",
        "injected_battery_percent": "" if injected is None else injected,
        "voltage_V": "" if voltage is None else voltage,
        "current_A": "" if current is None else current,
    }


class AhWhIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="aer_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_ah_integration_known_constant_current(self):
        # 3 samples, 1s apart, constant 2A -> trapezoid over 2 segments of
        # (2+2)/2*1 = 2 A*s each = 4 A*s total = 4/3600 Ah.
        rows = [_row(0.0, current=2.0, voltage=12.0),
                _row(1.0, current=2.0, voltage=12.0),
                _row(2.0, current=2.0, voltage=12.0)]
        run_dir = _write_run(self.tmpdir, "run-ah", rows)
        result = aer.analyze(run_dir)
        # Output values are rounded to 4 decimal places -- compare at that
        # same precision, not exact float equality.
        self.assertAlmostEqual(result["energy"]["integrated_Ah"], 4.0 / 3600.0, places=4)

    def test_wh_integration_known_voltage_current_time(self):
        # Same 3 samples: V*I = 24W constant -> 2 segments of 24*1=24 Ws each
        # = 48 Ws = 48/3600 Wh.
        rows = [_row(0.0, current=2.0, voltage=12.0),
                _row(1.0, current=2.0, voltage=12.0),
                _row(2.0, current=2.0, voltage=12.0)]
        run_dir = _write_run(self.tmpdir, "run-wh", rows)
        result = aer.analyze(run_dir)
        self.assertAlmostEqual(result["energy"]["integrated_Wh"], 48.0 / 3600.0, places=4)

    def test_ah_integration_ramping_current_trapezoid(self):
        # 0A -> 4A over 1s (trapezoid: (0+4)/2*1 = 2 A*s), then 4A -> 4A over
        # 1s (4 A*s) = 6 A*s total = 6/3600 Ah. Proves the trapezoid formula
        # (not a naive rectangle rule) is what's actually used.
        rows = [_row(0.0, current=0.0, voltage=12.0),
                _row(1.0, current=4.0, voltage=12.0),
                _row(2.0, current=4.0, voltage=12.0)]
        run_dir = _write_run(self.tmpdir, "run-ramp", rows)
        result = aer.analyze(run_dir)
        self.assertAlmostEqual(result["energy"]["integrated_Ah"], 6.0 / 3600.0, places=4)

    def test_irregular_sample_intervals_use_actual_dt(self):
        # dt = 0.5s then dt = 3.0s (both under the default gap threshold),
        # constant 1A -> (1+1)/2*0.5 + (1+1)/2*3.0 = 0.5 + 3.0 = 3.5 A*s.
        rows = [_row(0.0, current=1.0, voltage=10.0),
                _row(0.5, current=1.0, voltage=10.0),
                _row(3.5, current=1.0, voltage=10.0)]
        run_dir = _write_run(self.tmpdir, "run-irregular", rows)
        result = aer.analyze(run_dir, gap_threshold_s=5.0)
        self.assertAlmostEqual(result["energy"]["integrated_Ah"], 3.5 / 3600.0, places=4)

    def test_missing_current_samples_excluded_not_zero(self):
        # Middle sample has no current reading -- both segments touching it
        # must be skipped, not treated as 0A.
        rows = [_row(0.0, current=2.0, voltage=12.0),
                _row(1.0, current=None, voltage=12.0),
                _row(2.0, current=2.0, voltage=12.0)]
        run_dir = _write_run(self.tmpdir, "run-missing-i", rows)
        result = aer.analyze(run_dir)
        # No valid consecutive pair -> nothing integrated.
        self.assertIsNone(result["energy"]["integrated_Ah"])
        self.assertEqual(result["data_quality"]["valid_current_sample_count"], 2)

    def test_missing_voltage_samples_excluded_from_wh_not_ah(self):
        # Voltage missing on one sample must exclude that segment from Wh
        # but must NOT affect Ah (current-only integration).
        rows = [_row(0.0, current=2.0, voltage=12.0),
                _row(1.0, current=2.0, voltage=None),
                _row(2.0, current=2.0, voltage=12.0)]
        run_dir = _write_run(self.tmpdir, "run-missing-v", rows)
        result = aer.analyze(run_dir)
        self.assertAlmostEqual(result["energy"]["integrated_Ah"], 4.0 / 3600.0, places=4)
        # Both Wh segments touch the voltage-missing sample -> 0 Wh segments used.
        self.assertIsNone(result["energy"]["integrated_Wh"])

    def test_excessive_gap_is_skipped_and_reported_not_bridged(self):
        rows = [_row(0.0, current=2.0, voltage=12.0),
                _row(1.0, current=2.0, voltage=12.0),
                # 100s gap -- must not be bridged as if continuous.
                _row(101.0, current=2.0, voltage=12.0),
                _row(102.0, current=2.0, voltage=12.0)]
        run_dir = _write_run(self.tmpdir, "run-gap", rows)
        result = aer.analyze(run_dir, gap_threshold_s=5.0)
        # Only the two 1s segments integrate: 2 + 2 = 4 A*s = 4/3600 Ah.
        self.assertAlmostEqual(result["energy"]["integrated_Ah"], 4.0 / 3600.0, places=4)
        gaps = result["data_quality"]["gaps_skipped_from_energy_integration"]
        self.assertEqual(len(gaps), 1)
        self.assertAlmostEqual(gaps[0]["dt_s"], 100.0, places=2)
        self.assertEqual(result["data_quality"]["largest_telemetry_gap_s"], 100.0)


class GpsDistanceTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="aer_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_gps_distance_reconstruction_known_points(self):
        # Two points exactly 0.1 deg latitude apart on the equator: known
        # great-circle distance via haversine, cross-checked with geo.py's
        # own function (never a second, competing implementation).
        import geo
        lat1, lon1 = 0.0, 0.0
        lat2, lon2 = 0.1, 0.0
        expected_m = geo.haversine_m(lat1, lon1, lat2, lon2)
        rows = [_row(0.0, lat=lat1, lon=lon1, speed=1.0),
                _row(1.0, lat=lat2, lon=lon2, speed=1.0)]
        run_dir = _write_run(self.tmpdir, "run-dist", rows)
        result = aer.analyze(run_dir)
        # distance_m is rounded to 2 decimal places in the output.
        self.assertAlmostEqual(result["motion"]["distance_m"], expected_m, places=2)

    def test_distance_skips_pairs_missing_position(self):
        rows = [_row(0.0, lat=0.0, lon=0.0, speed=1.0),
                _row(1.0, lat=None, lon=None, speed=1.0),
                _row(2.0, lat=0.001, lon=0.0, speed=1.0)]
        run_dir = _write_run(self.tmpdir, "run-dist-missing", rows)
        result = aer.analyze(run_dir)
        self.assertEqual(result["motion"]["position_pairs_used"], 0)
        self.assertEqual(result["motion"]["position_pairs_skipped_invalid"], 2)
        self.assertEqual(result["motion"]["distance_m"], 0.0)


class BatterySourceDistinctionTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="aer_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_physical_battery_distinct_from_injected_and_voltage_current_preserved(self):
        # A synthetic battery_percent injection must never make the physical
        # voltage/current evidence disappear (POWER TELEMETRY REQUIREMENTS).
        rows = [_row(0.0, battery=80.0, injected=15.0, current=3.0, voltage=12.0),
                _row(1.0, battery=79.5, injected=15.0, current=3.0, voltage=12.0)]
        run_dir = _write_run(self.tmpdir, "run-inject", rows)
        result = aer.analyze(run_dir)
        self.assertTrue(result["data_quality"]["synthetic_battery_injection_present"])
        self.assertEqual(result["battery"]["start_physical_battery_percent"], 80.0)
        self.assertEqual(result["battery"]["end_physical_battery_percent"], 79.5)
        self.assertIsNotNone(result["energy"]["integrated_Ah"])
        self.assertIsNotNone(result["battery"]["start_voltage_V"])


class ModelComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="aer_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _known_run(self, current_A, voltage_V, n_seconds, distance_m, design_speed_mps=1.0,
                   conservative_current_A=9.0):
        # Build a run travelling distance_m at design_speed_mps over
        # n_seconds+1 samples (1 Hz), constant current/voltage.
        rows = []
        step = distance_m / n_seconds if n_seconds else 0.0
        # Roughly 1 metre of latitude ~ 111320m near the equator.
        deg_per_m = 1.0 / 111320.0
        for i in range(n_seconds + 1):
            rows.append(_row(float(i), lat=i * step * deg_per_m, lon=0.0,
                             speed=design_speed_mps, current=current_A, voltage=voltage_V))
        return _write_run(self.tmpdir, "run-model", rows, replan_values={
            "conservative_current_A": conservative_current_A, "design_speed_mps": design_speed_mps,
            "nominal_capacity_Ah": 40.0, "usable_capacity_factor": 0.8,
            "mission_reserve_fraction": 0.15, "rtl_reserve_fraction": 0.05,
        })

    def test_equivalent_current_calculation(self):
        # 100m at 1 m/s design speed -> predicted_duration_h = 100/1/3600.
        # measured_integrated_Ah is whatever the constant-2A run integrates
        # to; equivalent_average_current_A must equal
        # measured_Ah / predicted_duration_h.
        run_dir = self._known_run(current_A=2.0, voltage_V=12.0, n_seconds=100, distance_m=100.0)
        result = aer.analyze(run_dir)
        mc = result["model_comparison"]
        # Constant 2A the whole (short, no-gap) run, travelled at exactly the
        # design speed -> equivalent_average_current_A = measured_Ah /
        # (travelled_distance_m / design_speed_mps / 3600) must recover ~2A.
        # Tolerance accounts for the small (~0.1%) haversine-vs-flat
        # approximation in this test's synthetic lat/lon step, not any
        # imprecision in the tool itself.
        self.assertAlmostEqual(mc["equivalent_average_current_A"], 2.0, places=1)
        self.assertGreater(mc["equivalent_average_current_A"], 1.9)
        self.assertLess(mc["equivalent_average_current_A"], 2.1)

    def test_signed_error_positive_when_model_over_predicts(self):
        # conservative_current_A (9.0) is far above the actual measured
        # current (1.0A) -> the model predicts MORE Ah than measured ->
        # signed_error_Ah must be POSITIVE and clearly labelled as an
        # over-prediction, never an ambiguous sign.
        run_dir = self._known_run(current_A=1.0, voltage_V=12.0, n_seconds=50, distance_m=50.0,
                                  conservative_current_A=9.0)
        result = aer.analyze(run_dir)
        mc = result["model_comparison"]
        self.assertGreater(mc["predicted_consumption_Ah"], mc["measured_integrated_Ah"])
        self.assertGreater(mc["signed_error_Ah"], 0.0)
        self.assertIn("over-prediction", mc["signed_error_interpretation"])
        self.assertGreater(mc["conservative_ratio_predicted_over_measured"], 1.0)

    def test_signed_error_negative_when_model_under_predicts(self):
        # conservative_current_A set BELOW the actual measured current ->
        # the model predicts LESS Ah than measured -> signed_error_Ah must
        # be NEGATIVE and labelled as under-prediction.
        run_dir = self._known_run(current_A=15.0, voltage_V=12.0, n_seconds=50, distance_m=50.0,
                                  conservative_current_A=9.0)
        result = aer.analyze(run_dir)
        mc = result["model_comparison"]
        self.assertLess(mc["predicted_consumption_Ah"], mc["measured_integrated_Ah"])
        self.assertLess(mc["signed_error_Ah"], 0.0)
        self.assertIn("under-prediction", mc["signed_error_interpretation"])
        self.assertLess(mc["conservative_ratio_predicted_over_measured"], 1.0)

    def test_soc_estimate_labelled_not_ground_truth(self):
        rows = [_row(0.0, battery=90.0, current=1.0, voltage=12.0),
                _row(1.0, battery=85.0, current=1.0, voltage=12.0)]
        run_dir = _write_run(self.tmpdir, "run-soc", rows)
        result = aer.analyze(run_dir)
        soc = result["soc_comparison"]
        self.assertEqual(soc["observed_soc_drop_points"], 5.0)
        # 40.0 * 0.8 * 5/100 = 1.6 Ah
        self.assertAlmostEqual(soc["estimated_capacity_from_soc_drop_Ah"], 1.6, places=6)
        self.assertIn("NOT ground truth", soc["label"])


class ReadOnlyContractTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="aer_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _sha256_of_dir(self, run_dir):
        digests = {}
        for name in sorted(os.listdir(run_dir)):
            path = os.path.join(run_dir, name)
            if not os.path.isfile(path):
                continue
            h = hashlib.sha256()
            with open(path, "rb") as f:
                h.update(f.read())
            digests[name] = h.hexdigest()
        return digests

    def test_analysis_never_modifies_source_run_files(self):
        rows = [_row(0.0, current=2.0, voltage=12.0, lat=0.0, lon=0.0),
                _row(1.0, current=2.0, voltage=12.0, lat=0.001, lon=0.0)]
        run_dir = _write_run(self.tmpdir, "run-readonly", rows)
        before = self._sha256_of_dir(run_dir)
        before_listing = sorted(os.listdir(run_dir))

        out_path = os.path.join(self.tmpdir, "out.json")
        result = aer.analyze(run_dir)
        with open(out_path, "w") as f:
            json.dump(result, f)

        after = self._sha256_of_dir(run_dir)
        after_listing = sorted(os.listdir(run_dir))
        self.assertEqual(before, after)
        self.assertEqual(before_listing, after_listing)

    def test_default_output_path_is_sibling_not_inside_run_dir(self):
        rows = [_row(0.0, current=1.0, voltage=12.0)]
        run_dir = _write_run(self.tmpdir, "run-outpath", rows)
        out_path = aer._default_output_path(run_dir)
        self.assertEqual(os.path.dirname(out_path), os.path.dirname(run_dir))
        self.assertNotEqual(os.path.dirname(out_path), run_dir)

    def test_main_writes_output_and_does_not_touch_run_dir(self):
        rows = [_row(0.0, current=1.0, voltage=12.0),
                _row(1.0, current=1.0, voltage=12.0)]
        run_dir = _write_run(self.tmpdir, "run-main", rows)
        before_listing = sorted(os.listdir(run_dir))
        out_path = os.path.join(self.tmpdir, "explicit_out.json")
        rc = aer.main([run_dir, "--output", out_path, "--quiet"])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(out_path))
        with open(out_path) as f:
            data = json.load(f)
        self.assertEqual(data["schema_version"], "energy-analysis-v1")
        self.assertEqual(sorted(os.listdir(run_dir)), before_listing)


if __name__ == "__main__":
    unittest.main()
