import csv
import json
import unittest
from datetime import UTC, datetime, timedelta
from io import StringIO

from src.planning_report import export_planning_json, export_stop_order_csv, snapshot_suggested_stops


class PlanningReportTests(unittest.TestCase):
    def setUp(self):
        departure = datetime(2018, 7, 27, 16, tzinfo=UTC)
        self.run = {
            "run_id": "run-1", "route_id": "RouteID_test", "algorithm": "OR-Tools RoutingModel",
            "parameters": {"vehicles": 1}, "started_at_utc": "2026-11-09T12:00:00+00:00",
            "solver_status": "ROUTING_SUCCESS", "solve_seconds": 20, "search_budget_seconds": 20,
            "historical_metrics": {"driving_seconds": 10.5, "risk_package_count": 0},
            "suggested_metrics": {"driving_seconds": 8.5, "return_datetime_utc": "2018-07-27T16:00:20+00:00"},
            "suggested_order": ["VE", "A", "VE"],
        }
        self.review = {"run_id": "run-1", "decision": "REJECTED", "note": "需复核", "reviewed_at_utc": "2026-11-09T12:05:00+00:00"}
        self.schedule = {
            "departure_datetime_utc": departure,
            "return_datetime_utc": departure + timedelta(seconds=20),
            "stop_details": {"A": {
                "arrival_datetime_utc": departure + timedelta(seconds=3.5),
                "service_start_datetime_utc": departure + timedelta(seconds=5),
                "departure_datetime_utc": departure + timedelta(seconds=15),
            }},
        }
        self.stops = {"VE": {"type": "Station"}, "A": {"type": "Dropoff"}}

    def test_json_contains_saved_fields_review_and_standard_timestamps(self):
        snapshot = snapshot_suggested_stops(self.run["suggested_order"], self.stops, self.schedule)
        report = json.loads(export_planning_json({**self.run, "suggested_stops": snapshot}, self.review))
        for key, value in self.run.items():
            self.assertEqual(report[key], value)
        self.assertEqual(report["review"], self.review)
        self.assertEqual(report["record_type"], "local_planning_simulation")
        self.assertEqual(report["stop_timing_source"], "saved_snapshot")
        self.assertEqual(report["suggested_stops"][1]["arrival_datetime_utc"], "2018-07-27T16:00:03.500000+00:00")
        self.assertIsNone(report["suggested_stops"][0]["arrival_datetime_utc"])

    def test_csv_preserves_all_visits_ids_types_and_times(self):
        snapshot = snapshot_suggested_stops(self.run["suggested_order"], self.stops, self.schedule)
        rows = list(csv.DictReader(StringIO(export_stop_order_csv({**self.run, "suggested_stops": snapshot}))))
        self.assertEqual([(r["visit_number"], r["stop_id"], r["stop_type"]) for r in rows], [
            ("1", "VE", "Station"), ("2", "A", "Dropoff"), ("3", "VE", "Station"),
        ])
        self.assertTrue(all(r["run_id"] == "run-1" and r["route_id"] == "RouteID_test" for r in rows))
        self.assertEqual(rows[0]["departure_datetime_utc"], "2018-07-27T16:00:00+00:00")
        self.assertEqual(rows[1]["service_start_datetime_utc"], "2018-07-27T16:00:05+00:00")
        self.assertEqual(rows[1]["departure_datetime_utc"], "2018-07-27T16:00:15+00:00")
        self.assertEqual(rows[2]["arrival_datetime_utc"], "2018-07-27T16:00:20+00:00")
        self.assertEqual(rows[2]["departure_datetime_utc"], "")

    def test_old_snapshot_marks_missing_times_and_uses_saved_return_only(self):
        report = json.loads(export_planning_json(self.run, self.review))
        rows = list(csv.DictReader(StringIO(export_stop_order_csv(self.run))))
        self.assertEqual(report["stop_timing_source"], "return_time_only")
        self.assertIsNone(report["suggested_stops"][1]["arrival_datetime_utc"])
        self.assertEqual(rows[1]["arrival_datetime_utc"], "")
        self.assertEqual(rows[1]["stop_timing_source"], "return_time_only")
        self.assertEqual(rows[-1]["arrival_datetime_utc"], self.run["suggested_metrics"]["return_datetime_utc"])

    def test_failed_run_exports_no_suggested_route(self):
        failure = {**self.run, "solver_status": "ROUTING_FAIL_TIMEOUT", "suggested_order": None, "suggested_metrics": None}
        report = json.loads(export_planning_json(failure, self.review))
        self.assertIsNone(report["suggested_order"])
        self.assertIsNone(report["suggested_stops"])
        self.assertEqual(report["stop_timing_source"], "no_suggested_route")
        self.assertEqual(list(csv.DictReader(StringIO(export_stop_order_csv(failure)))), [])

    def test_json_rejects_nonstandard_nan(self):
        with self.assertRaises(ValueError):
            export_planning_json({**self.run, "solve_seconds": float("nan")}, self.review)
