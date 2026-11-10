import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from src.planning_db import get_review, get_run, list_runs, save_run, submit_review


class PlanningDatabaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "planning.sqlite3"
        self.run = {
            "run_id": "run-1",
            "route_id": "RouteID_test",
            "started_at_utc": "2026-11-09T12:00:00.000000+00:00",
            "solver_status": "ROUTING_SUCCESS",
            "solve_seconds": 20.001,
            "search_budget_seconds": 20,
            "algorithm": "OR-Tools RoutingModel",
            "parameters": {"vehicles": 1, "cost_scale": 1000},
            "suggested_order": ["VE", "B", "A", "VE"],
            "historical_metrics": {"driving_seconds": 100.5, "total_operation_seconds": 180.5},
            "suggested_metrics": {
                "driving_seconds": 90.5, "total_operation_seconds": 175.5,
                "waiting_seconds": 5, "risk_package_count": 0, "risk_stop_count": 0,
                "return_datetime_utc": "2018-07-27T16:05:20+00:00",
            },
            "error_message": None,
        }

    def test_inserts_and_queries_route_and_run_id(self):
        self.assertTrue(save_run(self.path, self.run))
        self.assertEqual(get_run(self.path, "run-1"), self.run)
        self.assertEqual([record["run_id"] for record in list_runs(self.path, "RouteID_test")], ["run-1"])
        self.assertEqual(list_runs(self.path, "' OR 1=1 --"), [])
        self.assertIsNone(get_run(self.path, "' OR 1=1 --"))

    def test_keeps_multiple_runs_for_one_route(self):
        second = {
            **self.run, "run_id": "run-2", "started_at_utc": "2026-11-09T12:01:00.000000+00:00",
            "suggested_order": ["VE", "A", "B", "VE"],
            "suggested_metrics": {"driving_seconds": 98.5},
        }
        save_run(self.path, self.run)
        save_run(self.path, second)
        self.assertEqual([row["run_id"] for row in list_runs(self.path, self.run["route_id"])], ["run-2", "run-1"])
        self.assertEqual(get_run(self.path, "run-1"), self.run)
        self.assertEqual(get_run(self.path, "run-2"), second)

    def test_retry_is_idempotent_and_conflicting_snapshot_cannot_overwrite(self):
        save_run(self.path, self.run)
        self.assertFalse(save_run(self.path, self.run))
        with self.assertRaisesRegex(ValueError, "different snapshot"):
            save_run(self.path, {**self.run, "solve_seconds": 18})
        self.assertEqual(get_run(self.path, "run-1"), self.run)

    def test_saves_failed_run_without_suggested_route(self):
        failure = {
            **self.run, "solver_status": "ROUTING_FAIL_TIMEOUT", "suggested_order": None,
            "suggested_metrics": None, "error_message": "No feasible route",
        }
        save_run(self.path, failure)
        self.assertEqual(get_run(self.path, "run-1"), failure)
        self.assertEqual(list_runs(self.path, failure["route_id"])[0]["solver_status"], "ROUTING_FAIL_TIMEOUT")

    def test_record_is_committed_and_connections_are_closed(self):
        save_run(self.path, self.run)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT run_id FROM planning_runs").fetchone(), ("run-1",))
        connection.close()
        self.assertEqual(get_run(self.path, "run-1"), self.run)

    def test_rejects_nonfinite_metrics_without_writing_a_row(self):
        run = {**self.run, "suggested_metrics": {"waiting_seconds": float("nan")}}
        with self.assertRaises(ValueError):
            save_run(self.path, run)
        self.assertIsNone(get_run(self.path, "run-1"))

    def test_approval_persists_and_keeps_the_planning_snapshot(self):
        save_run(self.path, self.run)
        self.assertEqual(get_review(self.path, "run-1"), {
            "run_id": "run-1", "decision": "PENDING", "reviewed_at_utc": None, "note": None,
        })
        approved = submit_review(self.path, "run-1", "APPROVED")
        self.assertEqual(approved["decision"], "APPROVED")
        self.assertEqual(approved["note"], "")
        self.assertEqual(datetime.fromisoformat(approved["reviewed_at_utc"]).utcoffset(), timedelta(0))
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT decision FROM planning_reviews").fetchone(), ("APPROVED",))
        self.assertEqual(get_review(self.path, "run-1"), approved)
        self.assertEqual(get_run(self.path, "run-1"), self.run)

    def test_rejection_requires_reason_and_keeps_it_verbatim_after_trimming(self):
        save_run(self.path, self.run)
        with self.assertRaisesRegex(ValueError, "必须填写原因"):
            submit_review(self.path, "run-1", "REJECTED", " \n\t")
        self.assertEqual(get_review(self.path, "run-1")["decision"], "PENDING")
        rejected = submit_review(self.path, "run-1", "REJECTED", "  总作业时间过长\n请复核  ")
        self.assertEqual(rejected["note"], "总作业时间过长\n请复核")
        self.assertEqual(get_review(self.path, "run-1"), rejected)
        self.assertEqual(get_run(self.path, "run-1"), self.run)

    def test_duplicate_review_is_idempotent_and_conflicting_review_is_refused(self):
        save_run(self.path, self.run)
        first = submit_review(self.path, "run-1", "APPROVED", "核对通过")
        self.assertEqual(submit_review(self.path, "run-1", "APPROVED", "核对通过"), first)
        for decision, note in (("APPROVED", "新备注"), ("REJECTED", "改判")):
            with self.subTest(decision=decision), self.assertRaisesRegex(ValueError, "最终审核决定"):
                submit_review(self.path, "run-1", decision, note)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM planning_reviews").fetchone(), (1,))

    def test_failed_run_cannot_be_approved_and_can_be_rejected(self):
        failure = {**self.run, "solver_status": "ROUTING_FAIL_TIMEOUT", "suggested_order": None, "suggested_metrics": None}
        save_run(self.path, failure)
        with self.assertRaisesRegex(ValueError, "没有可批准"):
            submit_review(self.path, "run-1", "APPROVED")
        self.assertEqual(get_review(self.path, "run-1")["decision"], "PENDING")
        self.assertEqual(submit_review(self.path, "run-1", "REJECTED", "未找到可行解")["decision"], "REJECTED")

    def test_migrates_old_rows_and_persists_stop_snapshots(self):
        save_run(self.path, self.run)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DROP TABLE planning_reviews")
            connection.execute("ALTER TABLE planning_runs DROP COLUMN suggested_stops_json")
        self.assertEqual(get_run(self.path, "run-1"), self.run)
        self.assertEqual(get_review(self.path, "run-1")["decision"], "PENDING")
        new_run = {**self.run, "run_id": "run-2", "suggested_stops": [{"stop_id": "VE"}]}
        self.assertTrue(save_run(self.path, new_run))
        self.assertEqual(get_run(self.path, "run-2"), new_run)
