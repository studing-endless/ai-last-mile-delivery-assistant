import json
import tempfile
import unittest
from datetime import UTC, date, datetime
from math import isnan
from pathlib import Path

from src.delivery_task import load_delivery_task
from src.travel_time_loader import calculate_driving_seconds


class DeliveryTaskTests(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.data_dir = Path(temp_dir.name)
        self.route_id = "RouteID_test"
        self.route = {
            "station_code": "DTEST",
            "date_YYYY_MM_DD": "2018-07-27",
            "departure_time_utc": "16:02:10",
            "stops": {
                "A": {"type": "Dropoff", "lat": 34.1, "zone_id": "A-1"},
                "VE": {"type": "Station", "lat": 34.0, "zone_id": float("nan")},
                "B": {"type": "Dropoff", "lat": 34.2, "zone_id": "B-1"},
            },
        }
        self.matrix = {
            "VE": {"VE": 0, "A": 10, "B": 15},
            "A": {"VE": 12, "B": 20.5},
            "B": {"VE": 18, "A": 13},
        }
        self.packages = {
            "A": {
                "P1": {
                    "scan_status": "DELIVERY_ATTEMPTED",
                    "planned_service_time_seconds": 12.5,
                    "dimensions": {"depth_cm": 10, "height_cm": 3, "width_cm": 7},
                    "time_window": {
                        "start_time_utc": "2018-07-27 17:00:00",
                        "end_time_utc": "2018-07-27 18:00:00",
                    },
                },
                "P2": {
                    "scan_status": "DELIVERED",
                    "planned_service_time_seconds": 8,
                    "time_window": {
                        "start_time_utc": "2018-07-27 19:00:00",
                        "end_time_utc": "2018-07-27 20:00:00",
                    },
                },
            },
            "B": {
                "P3": {
                    "scan_status": "DELIVERED",
                    "planned_service_time_seconds": 5,
                    "time_window": {"start_time_utc": float("nan"), "end_time_utc": float("nan")},
                },
            },
        }
        self.write("route_data.json", self.route)
        self.write("actual_sequences.json", {"actual": {"A": 1, "B": 2, "VE": 0}})
        self.write("travel_times.json", self.matrix)
        self.write("package_data.json", self.packages)

    def write(self, filename, route_data):
        data = json.dumps({self.route_id: route_data})
        (self.data_dir / filename).write_text(data, encoding="utf-8")

    def load(self):
        return load_delivery_task(self.route_id, self.data_dir)

    def test_assembles_route_schedule_stops_and_directed_matrix(self):
        task = self.load()

        self.assertEqual(task["route_id"], self.route_id)
        self.assertEqual(task["station_code"], "DTEST")
        self.assertEqual(task["station_id"], "VE")
        self.assertEqual(task["stop_count"], 3)
        self.assertEqual(task["stops"]["A"], self.route["stops"]["A"])
        self.assertTrue(isnan(task["stops"]["VE"]["zone_id"]))
        self.assertEqual(task["ordered_stops"], ["VE", "A", "B"])
        self.assertEqual(task["delivery_date"], date(2018, 7, 27))
        self.assertEqual(task["departure_time_utc"], "16:02:10")
        self.assertEqual(task["departure_datetime_utc"], datetime(2018, 7, 27, 16, 2, 10, tzinfo=UTC))
        self.assertIs(task["departure_datetime_utc"].tzinfo, UTC)
        self.assertEqual(task["travel_matrix"], self.matrix)
        self.assertEqual(calculate_driving_seconds(task["ordered_stops"], task["travel_matrix"]), 30.5)

    def test_preserves_package_details_and_conflicting_windows(self):
        by_stop = self.load()["packages_by_stop"]
        self.assertEqual(by_stop.keys(), self.route["stops"].keys())
        self.assertEqual(by_stop["VE"]["package_count"], 0)
        self.assertEqual(by_stop["A"]["package_count"], 2)
        self.assertEqual(by_stop["A"]["planned_service_time_seconds_sum"], 20.5)
        self.assertTrue(by_stop["A"]["has_empty_time_window_intersection"])
        self.assertIsNone(by_stop["A"]["time_window_intersection"])

        p1 = by_stop["A"]["packages"]["P1"]
        self.assertEqual(p1["scan_status"], "DELIVERY_ATTEMPTED")
        self.assertEqual(p1["dimensions"], self.packages["A"]["P1"]["dimensions"])
        self.assertEqual(p1["raw_time_window"], self.packages["A"]["P1"]["time_window"])
        expected_windows = {
            "P1": (datetime(2018, 7, 27, 17, tzinfo=UTC), datetime(2018, 7, 27, 18, tzinfo=UTC)),
            "P2": (datetime(2018, 7, 27, 19, tzinfo=UTC), datetime(2018, 7, 27, 20, tzinfo=UTC)),
        }
        self.assertEqual(by_stop["A"]["package_time_windows"], expected_windows)
        self.assertIsNone(by_stop["B"]["packages"]["P3"]["time_window"])

    def test_rejects_missing_or_invalid_departure_metadata(self):
        cases = (("2018-02-30", "16:02:10"), ("2018-07-27", "25:00:00"), ("2018-07-27", None))
        for date_text, time_text in cases:
            with self.subTest(date=date_text, departure_time=time_text):
                self.route["date_YYYY_MM_DD"] = date_text
                if time_text is None:
                    del self.route["departure_time_utc"]
                else:
                    self.route["departure_time_utc"] = time_text
                self.write("route_data.json", self.route)

                with self.assertRaisesRegex(ValueError, r"RouteID_test.*invalid departure metadata"):
                    self.load()

    def test_rejects_non_utc_offset_in_departure_field(self):
        self.route["departure_time_utc"] = "16:02:10+01:00"
        self.write("route_data.json", self.route)

        with self.assertRaisesRegex(ValueError, r"RouteID_test.*departure_time_utc must be UTC"):
            self.load()

    def test_rejects_missing_travel_leg(self):
        del self.matrix["A"]["B"]
        self.write("travel_times.json", self.matrix)

        with self.assertRaisesRegex(ValueError, r"RouteID_test.*destinations from 'A'.*missing=\['B'\]"):
            self.load()

    def test_rejects_package_stop_outside_route(self):
        self.packages["X"] = {"P4": self.packages["A"]["P1"]}
        self.write("package_data.json", self.packages)

        with self.assertRaisesRegex(ValueError, r"RouteID_test.*unknown stops.*'X'"):
            self.load()
