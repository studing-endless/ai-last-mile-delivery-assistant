import json
import tempfile
import unittest
from pathlib import Path

from src.travel_time_loader import calculate_driving_seconds, load_travel_matrix


class TravelTimeLoaderTests(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.data_dir = Path(temp_dir.name)
        self.path = self.data_dir / "travel_times.json"
        self.route_id = "RouteID_test"
        self.stop_ids = ["A", "B", "C"]
        self.matrix = {
            "A": {"B": 10.5, "C": 17},
            "B": {"A": 3, "C": 20},
            "C": {"A": 5, "B": 2},
        }

    def load(self):
        return load_travel_matrix(self.route_id, self.data_dir, self.stop_ids)

    def write_matrix(self):
        records = {"RouteID_other": {"X": {"X": 0}}, self.route_id: self.matrix}
        self.path.write_text(json.dumps(records), encoding="utf-8")

    def test_loads_selected_route_and_sums_directional_legs(self):
        self.write_matrix()
        matrix = self.load()

        self.assertEqual(matrix, self.matrix)
        self.assertEqual(calculate_driving_seconds(["A", "B", "C"], matrix), 30.5)
        self.assertEqual(calculate_driving_seconds(["C", "B", "A"], matrix), 5.0)

    def test_includes_return_leg_only_when_requested(self):
        self.write_matrix()
        matrix = self.load()
        stops = ["A", "B", "C"]

        self.assertEqual(calculate_driving_seconds(stops, matrix), 30.5)
        self.assertEqual(calculate_driving_seconds(stops, matrix, return_to_station=True), 35.5)
        self.assertEqual(calculate_driving_seconds([*stops, "A"], matrix, return_to_station=True), 35.5)

    def test_returns_zero_for_a_route_without_any_legs(self):
        for stops in ([], ["A"]):
            with self.subTest(stops=stops):
                self.assertEqual(calculate_driving_seconds(stops, {}, return_to_station=True), 0.0)

    def test_rejects_missing_route(self):
        self.path.write_text('{"RouteID_other": {}}', encoding="utf-8")

        with self.assertRaisesRegex(KeyError, r"RouteID_test.*travel_times\.json"):
            self.load()

    def test_rejects_missing_origin(self):
        del self.matrix["B"]
        self.write_matrix()

        with self.assertRaisesRegex(ValueError, r"origins.*missing=\['B'\]"):
            self.load()

    def test_rejects_unexpected_origin(self):
        self.matrix["D"] = {}
        self.write_matrix()

        with self.assertRaisesRegex(ValueError, r"origins.*extra=\['D'\]"):
            self.load()

    def test_rejects_missing_reverse_leg(self):
        del self.matrix["B"]["A"]
        self.write_matrix()

        with self.assertRaisesRegex(ValueError, r"destinations from 'B'.*missing=\['A'\]"):
            self.load()

    def test_rejects_unexpected_destination(self):
        self.matrix["A"]["D"] = 1
        self.write_matrix()

        with self.assertRaisesRegex(ValueError, r"destinations from 'A'.*extra=\['D'\]"):
            self.load()

    def test_rejects_invalid_travel_times(self):
        for seconds in (-1, "10", True, None):
            with self.subTest(seconds=seconds):
                self.matrix["A"]["B"] = seconds
                self.write_matrix()

                with self.assertRaisesRegex(ValueError, r"RouteID_test.*from 'A' to 'B'.*finite non-negative"):
                    self.load()

    def test_rejects_non_finite_json_values(self):
        for seconds in (float("nan"), float("inf")):
            with self.subTest(seconds=seconds):
                self.matrix["A"]["B"] = seconds
                self.write_matrix()

                with self.assertRaisesRegex(ValueError, r"RouteID_test.*travel_times\.json"):
                    self.load()

    def test_validates_optional_self_edge(self):
        self.matrix["A"]["A"] = 0
        self.write_matrix()
        self.assertEqual(self.load()["A"]["A"], 0.0)

        self.matrix["A"]["A"] = -1
        self.write_matrix()
        with self.assertRaisesRegex(ValueError, r"from 'A' to 'A'.*finite non-negative"):
            self.load()
