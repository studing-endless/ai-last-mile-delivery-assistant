import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evals.evaluate_routes import evaluate_sample, run_samples, stage_inputs, travel_matrices, write_results
from evals.route_sampling import build_metadata_index, select_samples
from evals.summarize_routes import audit_row, summarize_rows


class SamplingTests(unittest.TestCase):
    def test_frozen_order_and_empty_stratum_fallback_are_deterministic(self):
        index = [
            {"route_id": f"r-{stops}-{windows}-{position}", "stop_count": stops, "valid_time_window_count": windows}
            for stops in (95, 133, 159)
            for windows in (0, 10, 22, 48)
            if (stops, windows) != (95, 0)
            for position in range(2)
        ]
        samples, counts = select_samples(index)
        self.assertEqual((samples, counts), select_samples(list(reversed(index))))
        self.assertEqual(len({sample["route_id"] for sample in samples}), 12)
        self.assertEqual(sum(row["candidate_count"] for row in counts), len(index))
        self.assertEqual(counts[0]["candidate_count"], 0)
        self.assertEqual(samples[0]["target_stratum"], {"stop_count": "<=100", "valid_time_window_count": "0"})
        self.assertEqual(samples[0]["actual_stratum"], {"stop_count": "<=100", "valid_time_window_count": "1-15"})
        self.assertEqual([sample["stop_count"] for sample in samples[:3]], [95, 133, 159])

    def test_index_counts_valid_windows_missing_and_invalid_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "route_data.json").write_text(json.dumps({
                "r-a": {"stops": {"S": {}, "A": {}}, "route_score": "High"},
                "r-b": {"stops": {"S": {}}},
            }), encoding="utf-8")
            (data_dir / "package_data.json").write_text(json.dumps({
                "r-a": {"A": {
                    "p1": {"time_window": {"start_time_utc": "2026-01-01 00:00:00", "end_time_utc": "2026-01-01 01:00:00"}},
                    "p2": {"time_window": {"start_time_utc": float("nan"), "end_time_utc": float("nan")}},
                    "p3": {"time_window": {"start_time_utc": "2026-01-01 00:00:00", "end_time_utc": None}},
                }},
                "extra": {},
            }), encoding="utf-8")
            (first, missing), extra = build_metadata_index(data_dir)
        self.assertEqual((first["package_count"], first["valid_time_window_count"], first["invalid_time_window_count"]), (3, 1, 1))
        self.assertEqual((first["stop_count"], first["route_score"], extra), (2, "High", 1))
        self.assertIsNone(missing["package_count"])
        self.assertIsNone(missing["valid_time_window_count"])
        self.assertEqual(missing["metadata_status"], "missing_package_route")


class RouteEvaluationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data_dir = Path(folder.name)
        self.sample = {
            "route_id": "r-a", "position": 1, "route_score": "High", "stop_count": 3,
            "package_count": 2, "valid_time_window_count": 1,
        }
        route = {
            "station_code": "TEST", "date_YYYY_MM_DD": "2026-01-01", "departure_time_utc": "00:00:00",
            "stops": {"S": {"type": "Station"}, "A": {"type": "Dropoff"}, "B": {"type": "Dropoff"}},
        }
        matrix = {"S": {"A": 10, "B": 4}, "A": {"S": 4, "B": 10}, "B": {"S": 10, "A": 4}}
        packages = {
            "A": {"pa": {"planned_service_time_seconds": 5, "time_window": {
                "start_time_utc": "2026-01-01 00:00:20", "end_time_utc": "2026-01-01 00:01:00",
            }}},
            "B": {"pb": {"planned_service_time_seconds": 5}},
        }
        for name, entry in (
            ("route_data.json", route), ("actual_sequences.json", {"actual": {"S": 0, "A": 1, "B": 2}}),
            ("package_data.json", packages), ("travel_times.json", matrix),
        ):
            (self.data_dir / name).write_text(json.dumps({"other": entry, "r-a": entry}), encoding="utf-8")

    def test_batch_staging_reuses_the_loader_and_records_missing_route(self):
        stage_dir = self.data_dir / "stage"
        stage_dir.mkdir()
        solution = {
            "status": "ROUTING_SUCCESS", "found_solution": True, "suggested_order": ["S", "B", "A", "S"],
            "solve_seconds": 0.01, "historical_driving_seconds": 30.0, "suggested_driving_seconds": 12.0,
        }
        with patch("evals.evaluate_routes.travel_matrices", wraps=travel_matrices) as extract:
            staging = stage_inputs(self.data_dir, stage_dir, {"r-a", "missing"})
        extract.assert_called_once()
        for name, extraction in staging.items():
            self.assertEqual(extraction["missing_route_ids"], ["missing"])
            self.assertEqual(set(json.loads((stage_dir / name).read_bytes())), {"r-a"})
        with patch("evals.evaluate_routes.optimize_route", return_value=solution) as solve:
            row = evaluate_sample(self.sample, stage_dir)
            missing = evaluate_sample({**self.sample, "route_id": "missing"}, stage_dir)
        self.assertEqual(solve.call_count, 1)
        self.assertEqual(solve.call_args.kwargs, {"time_limit_seconds": 20})
        self.assertEqual(row["data_validation_status"], "passed")
        self.assertTrue(row["tour_verified"])
        self.assertIsNone(row["failure_reason"])
        self.assertEqual((row["historical_total_operation_seconds"], row["suggested_total_operation_seconds"]), (50, 29))
        self.assertEqual((row["historical_waiting_seconds"], row["suggested_waiting_seconds"]), (10, 7))
        self.assertEqual(row["driving_change_percent"], -60)
        self.assertEqual(row["total_operation_change_percent"], -42)
        self.assertEqual(missing["failure_stage"], "data_load")
        self.assertEqual(missing["solve_status"], "not_attempted")
        self.assertIsNone(missing["historical_driving_seconds"])

    def test_solver_failure_preserves_historical_metrics_and_serializes_nulls(self):
        with patch("evals.evaluate_routes.optimize_route", return_value={
            "status": "ROUTING_FAIL_TIMEOUT", "found_solution": False, "solve_seconds": 20,
        }):
            row = evaluate_sample(self.sample, self.data_dir)
        self.assertEqual(row["data_validation_status"], "passed")
        self.assertEqual(row["solve_status"], "failed")
        self.assertEqual(row["failure_stage"], "solve")
        self.assertEqual(row["historical_driving_seconds"], 30)
        self.assertIsNone(row["suggested_driving_seconds"])
        out = self.data_dir / "result"
        write_results(out, {"results": [row]})
        self.assertIsNone(json.loads((out / "results.json").read_bytes())["results"][0]["suggested_driving_seconds"])
        with (out / "results.csv").open(encoding="utf-8", newline="") as file:
            self.assertEqual(next(csv.DictReader(file))["suggested_driving_seconds"], "")

    def test_rejects_a_solver_tour_that_does_not_return_to_depot(self):
        with patch("evals.evaluate_routes.optimize_route", return_value={
            "status": "ROUTING_SUCCESS", "found_solution": True, "suggested_order": ["S", "A", "B"], "solve_seconds": 0.1,
        }):
            row = evaluate_sample(self.sample, self.data_dir)
        self.assertEqual(row["failure_stage"], "suggested_simulation")
        self.assertFalse(row["tour_verified"])
        self.assertIsNone(row["suggested_total_operation_seconds"])

    def test_resume_does_not_restage_or_resolve_completed_samples(self):
        second = {**self.sample, "position": 2, "route_id": "other"}
        output = self.data_dir / "resumable"
        finished = {"position": 1, "route_id": "r-a", "solve_seconds": 0.01, "failure_reason": None}
        failure = {"position": 2, "route_id": "other", "failure_stage": "solve", "failure_reason": "No route returned"}
        with patch("evals.evaluate_routes.stage_inputs", return_value={}):
            with patch("evals.evaluate_routes.evaluate_sample", side_effect=[finished, KeyboardInterrupt]):
                with self.assertRaises(KeyboardInterrupt):
                    run_samples([self.sample, second], self.data_dir, output, "fixture")
            checkpoint = json.loads((output / "results.json").read_bytes())
            self.assertEqual(checkpoint["results"], [finished])
            with patch("evals.evaluate_routes.evaluate_sample", return_value=failure) as evaluate:
                with patch("evals.evaluate_routes.stage_inputs", return_value={}) as stage:
                    report = run_samples([self.sample, second], self.data_dir, output, "fixture", resume=True)
                self.assertEqual(stage.call_args.args[2], {"other"})
                evaluate.assert_called_once()
                self.assertEqual(evaluate.call_args.args[0], second)
            with patch("evals.evaluate_routes.evaluate_sample") as evaluate:
                run_samples([self.sample, second], self.data_dir, output, "fixture", resume=True)
                evaluate.assert_not_called()
        self.assertEqual(report["results"], [finished, failure])
        with (output / "results.csv").open(encoding="utf-8", newline="") as file:
            self.assertEqual([row["position"] for row in csv.DictReader(file)], ["1", "2"])
        with self.assertRaisesRegex(ValueError, "Checkpoint does not match"):
            run_samples([self.sample, second], self.data_dir, output, "another-manifest", resume=True)

    def test_new_window_risk_is_detected_even_when_totals_do_not_change(self):
        packages_path = self.data_dir / "package_data.json"
        packages = json.loads(packages_path.read_bytes())
        for stop, package_id, end in (("A", "pa", 12), ("B", "pb", 20)):
            packages["r-a"][stop][package_id]["time_window"] = {
                "start_time_utc": "2026-01-01 00:00:00", "end_time_utc": f"2026-01-01 00:00:{end}",
            }
        packages_path.write_text(json.dumps(packages), encoding="utf-8")
        solution = {
            "status": "ROUTING_SUCCESS", "found_solution": True, "suggested_order": ["S", "B", "A", "S"],
            "solve_seconds": 0.01, "historical_driving_seconds": 30.0, "suggested_driving_seconds": 12.0,
        }
        sample = {**self.sample, "valid_time_window_count": 2}
        with patch("evals.evaluate_routes.optimize_route", return_value=solution):
            row = audit_row(evaluate_sample(sample, self.data_dir), sample)
        self.assertTrue(row["comparable"])
        for kind in ("package", "stop"):
            self.assertEqual(row[f"historical_risk_{kind}_count"], 1)
            self.assertEqual(row[f"suggested_risk_{kind}_count"], 1)
            self.assertEqual(row[f"new_risk_{kind}_count"], 1)
        stats = summarize_rows([row, {"comparable": False, "solve_status": "failed"}])
        self.assertEqual(stats["solver_success_rate"], 0.5)
        self.assertEqual(stats["driving"]["denominator"], 1)
        self.assertEqual(stats["risk_package"]["new_routes"], 1)
