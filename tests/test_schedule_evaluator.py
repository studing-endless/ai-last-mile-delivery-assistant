import unittest
from datetime import UTC, datetime, timedelta
from math import fsum

from src.schedule_evaluator import evaluate_schedule


class ScheduleEvaluatorTests(unittest.TestCase):
    departure = datetime(2018, 7, 27, 16, tzinfo=UTC)

    def at(self, seconds):
        return self.departure + timedelta(seconds=seconds)

    def summary(self, service, windows=None, intersection=None, conflict=False):
        return {
            "packages": {"P": {"planned_service_time_seconds": service}},
            "planned_service_time_seconds_sum": service,
            "package_time_windows": {} if windows is None else windows,
            "time_window_intersection": intersection,
            "has_empty_time_window_intersection": conflict,
        }

    def task(self, summaries, matrix):
        return {
            "station_id": "VE",
            "departure_datetime_utc": self.departure,
            "stops": {"VE": {"type": "Station"}, **{stop: {"type": "Dropoff"} for stop in summaries}},
            "packages_by_stop": {"VE": self.summary(0), **summaries},
            "travel_matrix": matrix,
        }

    def test_waits_for_window_and_counts_asymmetric_return_once(self):
        window = (self.at(30), self.at(60))
        task = self.task(
            {"A": self.summary(7.5, {"P1": window}, window), "B": self.summary(2.5)},
            {"VE": {"A": 10.25, "B": 8}, "A": {"VE": 11, "B": 5.75}, "B": {"VE": 21.5, "A": 6}},
        )
        result = evaluate_schedule(task, ["VE", "A", "B"])

        self.assertEqual(result, evaluate_schedule(task, ["VE", "A", "B", "VE"]))
        first, second = result["stop_details"]["A"], result["stop_details"]["B"]
        self.assertEqual(first["arrival_datetime_utc"], self.at(10.25))
        self.assertEqual(first["service_start_datetime_utc"], self.at(30))
        self.assertEqual(first["waiting_seconds"], 19.75)
        self.assertEqual(first["planned_service_time_seconds_sum"], 7.5)
        self.assertEqual(first["departure_datetime_utc"], self.at(37.5))
        self.assertEqual(second["arrival_datetime_utc"], self.at(43.25))
        self.assertEqual(second["service_start_datetime_utc"], self.at(43.25))
        self.assertEqual(second["departure_datetime_utc"], self.at(45.75))
        self.assertEqual(second["window_violations"], {})
        self.assertEqual(result["return_travel_seconds"], 21.5)
        self.assertEqual(result["return_datetime_utc"], self.at(67.25))
        self.assertEqual(result["driving_seconds"], 37.5)
        self.assertEqual(result["planned_service_time_seconds_sum"], 10.0)
        self.assertEqual(result["waiting_seconds"], 19.75)
        self.assertEqual(result["total_operation_seconds"], 67.25)
        self.assertEqual(result["risk_package_count"], 0)
        self.assertEqual(result["risk_stop_count"], 0)

    def test_includes_window_endpoint_and_detects_later_start_without_rounding(self):
        window = (self.at(5), self.at(10))
        matrix = {"VE": {"A": 10}, "A": {"VE": 3}}
        task = self.task({"A": self.summary(3, {"P1": window}, window)}, matrix)

        on_time = evaluate_schedule(task, ["VE", "A"])
        self.assertEqual(on_time["risk_package_count"], 0)
        self.assertEqual(on_time["stop_details"]["A"]["departure_datetime_utc"], self.at(13))

        matrix["VE"]["A"] = 10.0000002
        late = evaluate_schedule(task, ["VE", "A"])
        self.assertEqual(late["risk_package_count"], 1)
        self.assertEqual(late["risk_stop_count"], 1)
        self.assertEqual(late["waiting_seconds"], 0)
        self.assertEqual(late["stop_details"]["A"]["window_violations"], {
            "P1": {"reason": "after_window", "time_window": window},
        })

    def test_waits_for_shared_window_of_multiple_packages(self):
        first = (self.at(5), self.at(20))
        second = (self.at(15), self.at(30))
        task = self.task(
            {"A": self.summary(6, {"P1": first, "P2": second}, (self.at(15), self.at(20)))},
            {"VE": {"A": 10}, "A": {"VE": 4}},
        )
        result = evaluate_schedule(task, ["VE", "A"])

        self.assertEqual(result["stop_details"]["A"]["service_start_datetime_utc"], self.at(15))
        self.assertEqual(result["waiting_seconds"], 5)
        self.assertEqual(result["risk_package_count"], 0)
        self.assertEqual(result["window_conflict_stop_count"], 0)

    def test_package_service_subtotal_does_not_cause_false_window_risk(self):
        service_times = (0.1, 0.1, 0.1)
        first = self.summary(fsum(service_times))
        first["packages"] = {
            f"P{i}": {"planned_service_time_seconds": seconds}
            for i, seconds in enumerate(service_times)
        }
        window = (self.at(0), self.at(2))
        task = self.task(
            {"A": first, "B": self.summary(0, {"P": window}, window)},
            {"VE": {"A": 1}, "A": {"B": 0.7}, "B": {"VE": 3}},
        )
        result = evaluate_schedule(task, ["VE", "A", "B"])

        self.assertEqual(result["stop_details"]["A"]["planned_service_time_seconds_sum"], 0.3)
        self.assertEqual(result["stop_details"]["B"]["service_start_datetime_utc"], window[1])
        self.assertEqual(result["stop_details"]["B"]["window_violations"], {})
        self.assertEqual(result["risk_package_count"], 0)

    def test_reports_empty_intersection_and_checks_each_package_at_arrival(self):
        windows = {
            "P1": (self.at(0), self.at(5)),
            "P2": (self.at(15), self.at(20)),
            "P3": (self.at(5), self.at(15)),
        }
        task = self.task({"A": self.summary(6, windows, conflict=True)}, {"VE": {"A": 10}, "A": {"VE": 4}})
        result = evaluate_schedule(task, ["VE", "A"])
        stop = result["stop_details"]["A"]

        self.assertTrue(stop["has_empty_time_window_intersection"])
        self.assertEqual(stop["service_start_datetime_utc"], self.at(10))
        self.assertEqual(stop["waiting_seconds"], 0)
        self.assertEqual(stop["window_violations"], {
            "P1": {"reason": "after_window", "time_window": windows["P1"]},
            "P2": {"reason": "before_window", "time_window": windows["P2"]},
        })
        self.assertEqual(result["risk_package_count"], 2)
        self.assertEqual(result["risk_stop_count"], 1)
        self.assertEqual(result["window_conflict_stop_count"], 1)

    def test_handles_window_and_trip_across_utc_midnight(self):
        departure = datetime(2018, 7, 27, 23, 59, 50, tzinfo=UTC)
        window = (departure + timedelta(seconds=5), departure + timedelta(seconds=20))
        task = self.task(
            {"A": self.summary(8.5, {"P1": window}, window)}, {"VE": {"A": 15.25}, "A": {"VE": 6.25}},
        )
        task["departure_datetime_utc"] = departure
        result = evaluate_schedule(task, ["VE", "A", "VE"])

        self.assertEqual(result["risk_package_count"], 0)
        self.assertEqual(result["stop_details"]["A"]["arrival_datetime_utc"], datetime(2018, 7, 28, 0, 0, 5, 250000, UTC))
        self.assertEqual(result["return_datetime_utc"], datetime(2018, 7, 28, 0, 0, 20, tzinfo=UTC))
        self.assertEqual(result["total_operation_seconds"], 30)

    def test_rejects_incomplete_or_duplicate_visits(self):
        task = self.task({"A": self.summary(0), "B": self.summary(0)}, {})
        for order in (["VE", "A"], ["VE", "A", "A"], ["VE", "A", "VE", "B"], ["A", "B", "VE"]):
            with self.subTest(order=order), self.assertRaisesRegex(ValueError, r"Visit order must"):
                evaluate_schedule(task, order)
