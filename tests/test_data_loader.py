import json
import tempfile
import unittest
from pathlib import Path

from src.data_loader import load_route_and_sequence


class LoadRouteAndSequenceTests(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.data_dir = Path(temp_dir.name)
        self.route_id = "RouteID_test"
        self.stops = {
            "AB": {"type": "Dropoff", "lat": 47.5, "lng": -122.3},
            "CD": {"type": "Station", "lat": 47.6, "lng": -122.4},
            "EF": {"type": "Dropoff", "lat": 47.7, "lng": -122.5},
        }
        self.actual = {"AB": 2, "CD": 0, "EF": 1}
        self.write_data("route_data.json", {
            self.route_id: {"station_code": "DTEST", "stops": self.stops},
        })
        self.write_sequence(self.actual)

    def write_data(self, filename, data):
        (self.data_dir / filename).write_text(json.dumps(data), encoding="utf-8")

    def write_sequence(self, actual):
        self.write_data("actual_sequences.json", {self.route_id: {"actual": actual}})

    def load(self):
        return load_route_and_sequence(self.route_id, self.data_dir)

    def test_loads_route_in_actual_order(self):
        result = self.load()

        self.assertEqual(result["route_id"], self.route_id)
        self.assertEqual(result["station_code"], "DTEST")
        self.assertEqual(result["stops"], self.stops)
        self.assertEqual(result["ordered_stops"], ["CD", "EF", "AB"])
        self.assertEqual(result["stop_count"], 3)
        self.assertEqual(result["station_count"], 1)
        self.assertEqual(result["dropoff_count"], 2)
        self.assertIsNone(result["date_YYYY_MM_DD"])
        self.assertIsNone(result["departure_time_utc"])

    def test_exposes_raw_departure_fields(self):
        self.write_data("route_data.json", {
            self.route_id: {
                "station_code": "DTEST",
                "date_YYYY_MM_DD": "2018-07-27",
                "departure_time_utc": "16:02:10",
                "stops": self.stops,
            },
        })

        result = self.load()
        self.assertEqual(result["date_YYYY_MM_DD"], "2018-07-27")
        self.assertEqual(result["departure_time_utc"], "16:02:10")

    def test_rejects_missing_route_data(self):
        self.write_data("route_data.json", {})

        with self.assertRaisesRegex(KeyError, r"RouteID_test.*route_data\.json"):
            self.load()

    def test_rejects_missing_route_sequence(self):
        self.write_data("actual_sequences.json", {})

        with self.assertRaisesRegex(KeyError, r"RouteID_test.*actual_sequences\.json"):
            self.load()

    def test_rejects_missing_actual_mapping(self):
        self.write_data("actual_sequences.json", {self.route_id: {}})

        with self.assertRaisesRegex(ValueError, r"RouteID_test.*'actual' mapping"):
            self.load()

    def test_rejects_different_stop_sets_of_equal_size(self):
        self.write_sequence({"GH": 2, "CD": 0, "EF": 1})

        with self.assertRaisesRegex(ValueError, r"missing=\['AB'\], extra=\['GH'\]"):
            self.load()

    def test_rejects_invalid_sequence_positions(self):
        for positions in [(0, 1, 1), (0, 1, 3), (-1, 0, 1), (0, 1.0, 2), (0, True, 2), (0, "1", 2)]:
            with self.subTest(positions=positions):
                self.write_sequence(dict(zip(("CD", "EF", "AB"), positions)))

                with self.assertRaisesRegex(ValueError, r"consecutive integers from 0 to N-1"):
                    self.load()

    def test_rejects_route_starting_at_dropoff(self):
        self.write_sequence({"AB": 0, "CD": 2, "EF": 1})

        with self.assertRaisesRegex(ValueError, r"RouteID_test.*start.*Station"):
            self.load()

    def test_rejects_route_without_station(self):
        self.stops["CD"]["type"] = "Dropoff"
        self.write_data("route_data.json", {
            self.route_id: {"station_code": "DTEST", "stops": self.stops},
        })

        with self.assertRaisesRegex(ValueError, r"RouteID_test.*exactly one 'Station'.*found 0"):
            self.load()

    def test_rejects_additional_station_after_first_stop(self):
        self.stops["EF"]["type"] = "Station"
        self.write_data("route_data.json", {
            self.route_id: {"station_code": "DTEST", "stops": self.stops},
        })

        with self.assertRaisesRegex(ValueError, r"RouteID_test.*exactly one 'Station'.*found 2"):
            self.load()

    def test_rejects_invalid_non_start_stop_type(self):
        for invalid_stop, type_label in [({"type": "Pickup"}, "'Pickup'"), ({}, "None"), (None, "None")]:
            with self.subTest(stop=invalid_stop):
                self.stops["EF"] = invalid_stop
                self.write_data("route_data.json", {
                    self.route_id: {"station_code": "DTEST", "stops": self.stops},
                })

                with self.assertRaisesRegex(ValueError, rf"RouteID_test.*'EF'.*invalid type {type_label}"):
                    self.load()

    def test_rejects_empty_route(self):
        self.write_data("route_data.json", {
            self.route_id: {"station_code": "DTEST", "stops": {}},
        })
        self.write_sequence({})

        with self.assertRaisesRegex(ValueError, r"RouteID_test.*exactly one 'Station'.*found 0"):
            self.load()
