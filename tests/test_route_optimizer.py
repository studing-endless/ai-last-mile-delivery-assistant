import unittest
from unittest.mock import patch

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from src.route_optimizer import optimize_route


class RouteOptimizerTests(unittest.TestCase):
    def setUp(self):
        self.task = {
            "station_id": "VE",
            "stops": {"A": {"type": "Dropoff"}, "VE": {"type": "Station"}, "B": {"type": "Dropoff"}},
            "ordered_stops": ["VE", "A", "B"],
            "travel_matrix": {
                "VE": {"A": 0.8, "B": 0.1054},
                "A": {"VE": 0.3154, "B": 0.7},
                "B": {"VE": 0.9, "A": 0.2046},
            },
        }

    def test_finds_closed_directed_route_and_evaluates_original_seconds(self):
        result = optimize_route(self.task, time_limit_seconds=0.2)

        self.assertTrue(result["found_solution"])
        self.assertEqual(result["suggested_order"], ["VE", "B", "A", "VE"])
        self.assertEqual(self.task["ordered_stops"], ["VE", "A", "B"])
        self.assertAlmostEqual(result["suggested_driving_seconds"], 0.6254)
        self.assertAlmostEqual(result["historical_driving_seconds"], 2.4)
        self.assertAlmostEqual(result["saved_seconds"], 1.7746)
        self.assertAlmostEqual(result["saved_percent"], 100 * 1.7746 / 2.4)
        self.assertGreaterEqual(result["solve_seconds"], 0)

    def test_failure_returns_solver_status_and_no_suggested_metrics(self):
        timeout = routing_enums_pb2.RoutingSearchStatus.ROUTING_FAIL_TIMEOUT
        with (
            patch.object(pywrapcp.RoutingModel, "SolveWithParameters", return_value=None) as solve,
            patch.object(pywrapcp.RoutingModel, "status", return_value=timeout),
        ):
            result = optimize_route(self.task)

        self.assertEqual(solve.call_args.args[0].time_limit.seconds, 20)
        self.assertEqual(result["status"], "ROUTING_FAIL_TIMEOUT")
        self.assertFalse(result["found_solution"])
        self.assertAlmostEqual(result["historical_driving_seconds"], 2.4)
        for key in ("suggested_order", "suggested_driving_seconds", "saved_seconds", "saved_percent"):
            self.assertIsNone(result[key])

    def test_station_only_has_no_driving_and_no_percentage_baseline(self):
        task = {
            "station_id": "VE",
            "stops": {"VE": {"type": "Station"}},
            "ordered_stops": ["VE"],
            "travel_matrix": {"VE": {}},
        }
        result = optimize_route(task, time_limit_seconds=0.2)

        self.assertTrue(result["found_solution"])
        self.assertEqual(result["suggested_order"], ["VE", "VE"])
        self.assertEqual(result["suggested_driving_seconds"], 0.0)
        self.assertEqual(result["historical_driving_seconds"], 0.0)
        self.assertIsNone(result["saved_percent"])
