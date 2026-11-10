import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from src.ai_tools import TOOL_DEFINITIONS, compare_planning_run, dispatch_tool, get_planning_run, list_planning_runs
from src.planning_db import get_review, get_run, save_run, submit_review


class PlanningToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "planning.sqlite3"
        risks = {"risk_package_count": 0, "risk_stop_count": 0, "window_conflict_stop_count": 0}
        self.run = {
            "run_id": "run-a", "route_id": "RouteID_test", "started_at_utc": "2026-11-09T12:00:00+00:00",
            "solver_status": "ROUTING_SUCCESS", "solve_seconds": 20, "search_budget_seconds": 20,
            "algorithm": "OR-Tools RoutingModel", "parameters": {"vehicles": 1}, "error_message": None,
            "suggested_order": ["VE", "A", "VE"],
            "historical_metrics": {
                "driving_seconds": 80.1, "planned_service_time_seconds_sum": 20,
                "waiting_seconds": 0, "total_operation_seconds": 100.1, **risks,
            },
            "suggested_metrics": {
                "driving_seconds": 70.2, "planned_service_time_seconds_sum": 20,
                "waiting_seconds": 12.3, "total_operation_seconds": 102.5, **risks,
            },
        }
        save_run(self.path, self.run)

    def test_lists_only_the_requested_amazon_route(self):
        save_run(self.path, {**self.run, "route_id": "RouteID_other", "run_id": "run-b"})
        response = dispatch_tool("list_planning_runs", {"route_id": "RouteID_test"}, db_path=self.path)
        self.assertTrue(response["ok"])
        self.assertEqual(response["data"]["route_id"], "RouteID_test")
        self.assertEqual([run["run_id"] for run in response["data"]["runs"]], ["run-a"])
        self.assertEqual(response["source"]["run_id_field"], "data.runs[].run_id")
        self.assertEqual(response["source"]["route_id"], "RouteID_test")
        self.assertEqual(response["data"]["latest_run_id"], "run-a")
        self.assertEqual(response["data"]["record_count"], 1)
        empty = list_planning_runs("' OR 1=1 --", db_path=self.path)["data"]
        self.assertEqual(empty["runs"], [])
        self.assertEqual(empty["record_count"], 0)
        self.assertIsNone(empty["latest_run_id"])

    def test_list_sorts_actual_utc_instants_and_retains_stored_fields(self):
        older = {**self.run, "run_id": "run-b", "started_at_utc": "2026-11-09T13:00:00+02:00"}
        newest = {**self.run, "run_id": "run-c", "started_at_utc": "2026-11-09T07:01:00-05:00"}
        save_run(self.path, older)
        save_run(self.path, newest)

        data = list_planning_runs("RouteID_test", db_path=self.path)["data"]
        self.assertEqual([run["run_id"] for run in data["runs"]], ["run-c", "run-a", "run-b"])
        self.assertEqual(data["latest_run_id"], "run-c")
        self.assertEqual(data["record_count"], 3)
        self.assertEqual(data["runs"][0]["started_at_utc"], newest["started_at_utc"])
        self.assertEqual(data["sort_order"][0]["compare_as"], "utc_datetime")

    def test_list_breaks_timestamp_ties_by_descending_run_id(self):
        for run_id, timestamp in (
            ("run-c", "2026-11-09T09:00:00-03:00"),
            ("run-b", self.run["started_at_utc"]),
            ("run-d", "2026-11-09T12:00:00"),
        ):
            save_run(self.path, {**self.run, "run_id": run_id, "started_at_utc": timestamp})
        data = list_planning_runs("RouteID_test", db_path=self.path)["data"]
        self.assertEqual([run["run_id"] for run in data["runs"]], ["run-d", "run-c", "run-b", "run-a"])
        self.assertEqual(data["latest_run_id"], "run-d")
        self.assertEqual(data["sort_order"][1], {"field": "run_id", "direction": "descending"})

    def test_list_retains_missing_and_invalid_times_without_guessing_latest(self):
        for run_id, timestamp in (("run-b", ""), ("run-c", "unavailable"), ("run-d", "2026-11-10")):
            save_run(self.path, {**self.run, "run_id": run_id, "started_at_utc": timestamp})
        data = list_planning_runs("RouteID_test", db_path=self.path)["data"]
        self.assertEqual([run["run_id"] for run in data["runs"]], ["run-a", "run-d", "run-c", "run-b"])
        self.assertEqual(data["record_count"], 4)
        self.assertEqual(data["undated_record_count"], 3)
        self.assertEqual(data["latest_run_id"], "run-a")

        with patch("src.ai_tools.list_runs", return_value=[{"run_id": "legacy-run", "started_at_utc": None}]):
            legacy = list_planning_runs("RouteID_test", db_path=self.path)["data"]
        self.assertEqual(legacy["runs"], [{"run_id": "legacy-run", "started_at_utc": None}])
        self.assertEqual(legacy["record_count"], 1)
        self.assertEqual(legacy["undated_record_count"], 1)
        self.assertIsNone(legacy["latest_run_id"])

    def test_reads_saved_snapshot_and_review(self):
        review = submit_review(self.path, "run-a", "REJECTED", "耗时增加")
        result = get_planning_run("run-a", db_path=self.path)
        data = result["data"]
        self.assertEqual(data["suggested_order"], ["VE", "A", "VE"])
        self.assertEqual(data["suggested_metrics"]["driving_seconds"], 70.2)
        self.assertEqual(data["review"], review)
        self.assertIsNone(data["suggested_stops"])
        self.assertEqual(data["units"]["risk_package_count"], "packages")
        self.assertEqual(result["source"]["run_id"], "run-a")
        comparison = compare_planning_run("run-a", db_path=self.path)
        self.assertEqual(comparison["source"], result["source"])
        self.assertEqual(comparison["data"]["review"], review)

    def test_compares_signed_deltas_and_flags_longer_total(self):
        result = compare_planning_run("run-a", db_path=self.path)
        self.assertEqual(result["scope"], "offline_simulation")
        data = result["data"]
        self.assertEqual(data["delta_direction"], "suggested_minus_historical")
        self.assertEqual(data["metrics"]["driving_seconds"], {"historical": 80.1, "suggested": 70.2, "delta": -9.9, "unit": "seconds"})
        self.assertEqual(
            data["metrics"]["planned_service_time_seconds_sum"],
            {"historical": 20, "suggested": 20, "delta": 0, "unit": "seconds"},
        )
        self.assertEqual(data["metrics"]["waiting_seconds"]["delta"], 12.3)
        self.assertEqual(data["metrics"]["total_operation_seconds"]["delta"], 2.4)
        self.assertEqual(data["assessment"]["code"], "no_basis_to_replace")

    def test_shorter_total_allows_review_but_increased_risk_takes_priority(self):
        better = {**self.run["suggested_metrics"], "total_operation_seconds": 90}
        save_run(self.path, {**self.run, "run_id": "run-b", "suggested_metrics": better})
        risky = {**better, "risk_package_count": 2, "risk_stop_count": 1}
        save_run(self.path, {**self.run, "run_id": "run-c", "suggested_metrics": risky})
        self.assertEqual(compare_planning_run("run-b", db_path=self.path)["data"]["assessment"]["code"], "ready_for_human_review")
        risk = compare_planning_run("run-c", db_path=self.path)["data"]["assessment"]
        self.assertEqual(risk["code"], "window_risk_increased")
        self.assertEqual(risk["increased_risk_metrics"], ["risk_package_count", "risk_stop_count"])
        self.assertEqual(get_review(self.path, "run-b")["decision"], "PENDING")

    def test_missing_metrics_and_failed_run_remain_null(self):
        save_run(self.path, {**self.run, "run_id": "run-b", "suggested_metrics": {"driving_seconds": 5}})
        partial = compare_planning_run("run-b", db_path=self.path)["data"]
        self.assertEqual(partial["assessment"]["code"], "insufficient_data")
        self.assertIn("total_operation_seconds", partial["assessment"]["missing_metrics"])
        self.assertIsNone(partial["metrics"]["waiting_seconds"]["suggested"])
        self.assertIsNone(partial["metrics"]["waiting_seconds"]["delta"])
        self.assertEqual(
            partial["metrics"]["planned_service_time_seconds_sum"],
            {"historical": 20, "suggested": None, "delta": None, "unit": "seconds"},
        )
        save_run(self.path, {**self.run, "run_id": "run-c", "solver_status": "ROUTING_FAIL_TIMEOUT", "suggested_order": None, "suggested_metrics": None})
        failure = get_planning_run("run-c", db_path=self.path)["data"]
        self.assertIsNone(failure["suggested_order"])
        self.assertIsNone(failure["suggested_metrics"])

    def test_missing_run_and_unavailable_database_return_errors(self):
        for handler in (get_planning_run, compare_planning_run):
            result = handler("missing", db_path=self.path)
            self.assertEqual(result["error"]["code"], "run_not_found")
            self.assertEqual(result["source"]["run_id"], "missing")
        missing_db = self.path.parent / "absent" / "planning.sqlite3"
        self.assertEqual(list_planning_runs("RouteID_test", db_path=missing_db)["error"]["code"], "query_failed")
        self.assertFalse(missing_db.parent.exists())

    def test_dispatch_whitelist_and_parameter_validation(self):
        self.assertEqual({tool["name"] for tool in TOOL_DEFINITIONS}, {
            "list_planning_runs", "get_planning_run", "compare_planning_run",
        })
        for name in ("submit_review", "exec", "save_run", ["get_planning_run"]):
            self.assertEqual(dispatch_tool(name, {}, db_path=self.path)["error"]["code"], "unknown_tool")
        for arguments in (None, [], {}, {"run_id": 42}, {"run_id": "  "}, {"run_id": "x" * 257},
                          {"route_id": "RouteID_test"}, {"run_id": "run-a", "db_path": "other.sqlite3"}):
            with self.subTest(arguments=arguments):
                self.assertEqual(dispatch_tool("get_planning_run", arguments, db_path=self.path)["error"]["code"], "invalid_arguments")

    def test_nonfinite_legacy_values_are_serialized_as_null(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "UPDATE planning_runs SET parameters_json = ?, suggested_metrics_json = ? WHERE run_id = ?",
                ('{"values":[NaN, Infinity]}', '{"driving_seconds": NaN, "waiting_seconds": 1e999}', "run-a"),
            )
        for tool, args in (("list_planning_runs", {"route_id": "RouteID_test"}), ("get_planning_run", {"run_id": "run-a"}),
                           ("compare_planning_run", {"run_id": "run-a"}), ("get_planning_run", {"run_id": "missing"})):
            json.dumps(dispatch_tool(tool, args, db_path=self.path), allow_nan=False)
        data = get_planning_run("run-a", db_path=self.path)["data"]
        self.assertEqual(data["parameters"]["values"], [None, None])
        self.assertIsNone(data["suggested_metrics"]["driving_seconds"])
        self.assertIsNone(data["suggested_metrics"]["waiting_seconds"])
        metrics = compare_planning_run("run-a", db_path=self.path)["data"]["metrics"]
        self.assertIsNone(metrics["driving_seconds"]["delta"])
        self.assertIsNone(metrics["waiting_seconds"]["delta"])

    def test_reads_do_not_use_database_setup_or_change_rows(self):
        review = submit_review(self.path, "run-a", "APPROVED", "人工记录")
        before = self.path.read_bytes()
        with patch("src.planning_db._has_stop_snapshots", side_effect=AssertionError("Unexpected migration")):
            self.assertTrue(list_planning_runs("RouteID_test", db_path=self.path)["ok"])
            self.assertTrue(get_planning_run("run-a", db_path=self.path)["ok"])
            self.assertTrue(compare_planning_run("run-a", db_path=self.path)["ok"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(get_run(self.path, "run-a"), self.run)
        self.assertEqual(get_review(self.path, "run-a"), review)
