import json
import tempfile
import unittest
from datetime import UTC, datetime
from io import StringIO
from math import isnan
from pathlib import Path
from unittest.mock import patch

from src.package_loader import load_route_packages


def package(service=10, window=None, status="DELIVERED"):
    return {
        "scan_status": status,
        "planned_service_time_seconds": service,
        "dimensions": {"depth_cm": 12, "height_cm": 3, "width_cm": 8},
        "time_window": window,
    }


def window(start, end):
    return {"start_time_utc": start, "end_time_utc": end}


class PackageLoaderTests(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.data_dir = Path(temp_dir.name)
        self.path = self.data_dir / "package_data.json"
        self.route_id = "RouteID_target"
        self.stop_ids = ["VE", "A"]

    def write_route(self, route):
        self.path.write_text(json.dumps({self.route_id: route}), encoding="utf-8")

    def load(self):
        return load_route_packages(self.route_id, self.data_dir, self.stop_ids)

    def test_preserves_package_records_and_summarizes_stops(self):
        attempted = package(12.5, status="DELIVERY_ATTEMPTED")
        self.write_route({"A": {"P1": attempted, "P2": package(0)}})
        result = self.load()

        self.assertEqual(result["route_id"], self.route_id)
        self.assertEqual(result["stops"]["VE"]["package_count"], 0)
        self.assertEqual(result["stops"]["VE"]["planned_service_time_seconds_sum"], 0.0)
        stop = result["stops"]["A"]
        self.assertEqual(stop["package_count"], 2)
        self.assertEqual(stop["planned_service_time_seconds_sum"], 12.5)
        self.assertEqual(stop["packages"]["P1"], {
            "package_id": "P1",
            "scan_status": "DELIVERY_ATTEMPTED",
            "planned_service_time_seconds": 12.5,
            "dimensions": attempted["dimensions"],
            "raw_time_window": None,
            "time_window": None,
        })
        self.assertFalse(stop["has_empty_time_window_intersection"])

    def test_normalizes_nan_and_missing_windows_without_changing_raw_values(self):
        self.write_route({"A": {
            "P1": package(window=window(float("nan"), float("nan")), status="literal NaN"),
            "P2": package(window=float("nan")),
            "P3": package(window={}),
        }})
        stop = self.load()["stops"]["A"]

        for record in stop["packages"].values():
            self.assertIsNone(record["time_window"])
        self.assertTrue(isnan(stop["packages"]["P1"]["raw_time_window"]["start_time_utc"]))
        self.assertEqual(stop["packages"]["P1"]["scan_status"], "literal NaN")
        self.assertEqual(stop["package_time_windows"], {})
        self.assertIsNone(stop["time_window_intersection"])

    def test_rejects_invalid_service_time(self):
        for service in (-1, float("nan"), float("inf"), "10", True, None):
            with self.subTest(service=service):
                self.write_route({"A": {"P1": package(service)}})

                with self.assertRaisesRegex(ValueError, r"RouteID_target.*stop 'A'.*package 'P1'.*finite and non-negative"):
                    self.load()

    def test_rejects_invalid_and_date_only_timestamps(self):
        for start in ("2018-02-30 10:00:00", "2018-07-27", "NaN"):
            with self.subTest(start=start):
                self.write_route({"A": {"P1": package(window=window(start, "2018-07-27 12:00:00"))}})

                with self.assertRaisesRegex(ValueError, r"package 'P1'.*invalid start_time_utc"):
                    self.load()

    def test_rejects_reversed_or_zero_length_windows(self):
        for end in ("2018-07-27 08:00:00", "2018-07-27 09:00:00"):
            with self.subTest(end=end):
                self.write_route({"A": {"P1": package(window=window("2018-07-27 09:00:00", end))}})

                with self.assertRaisesRegex(ValueError, r"package 'P1'.*start must be before its end"):
                    self.load()

    def test_rejects_an_incomplete_window(self):
        self.write_route({"A": {"P1": package(window=window("2018-07-27 09:00:00", float("nan")))}})

        with self.assertRaisesRegex(ValueError, r"package 'P1'.*incomplete time window"):
            self.load()

    def test_summarizes_multiple_windows_and_preserves_original_values(self):
        first = window("2018-07-27 09:00:00", "2018-07-27T13:00:00+01:00")
        second = window("2018-07-27T10:00:00Z", "2018-07-27 13:00:00")
        self.write_route({"A": {"P1": package(window=first), "P2": package(window=second)}})

        stop = self.load()["stops"]["A"]
        nine, ten, noon, thirteen = (datetime(2018, 7, 27, hour, tzinfo=UTC) for hour in (9, 10, 12, 13))
        self.assertEqual(stop["package_time_windows"], {"P1": (nine, noon), "P2": (ten, thirteen)})
        self.assertIs(stop["package_time_windows"]["P1"][0].tzinfo, UTC)
        self.assertEqual(stop["packages"]["P1"]["raw_time_window"], first)
        self.assertEqual(stop["time_window_intersection"], (ten, noon))
        self.assertFalse(stop["has_empty_time_window_intersection"])

    def test_reports_disjoint_windows_and_includes_a_shared_endpoint(self):
        first = window("2018-07-27 08:00:00", "2018-07-27 09:00:00")
        for hour, expected_empty in ((9, False), (10, True)):
            with self.subTest(hour=hour):
                second = window(f"2018-07-27 {hour:02d}:00:00", "2018-07-27 11:00:00")
                self.write_route({"A": {"P1": package(window=first), "P2": package(window=second)}})

                stop = self.load()["stops"]["A"]
                self.assertEqual(stop["has_empty_time_window_intersection"], expected_empty)
                self.assertEqual(list(stop["package_time_windows"]), ["P1", "P2"])
                boundary = datetime(2018, 7, 27, 9, tzinfo=UTC)
                self.assertEqual(stop["time_window_intersection"], None if expected_empty else (boundary, boundary))

    def test_rejects_unknown_stop(self):
        self.write_route({"Z": {"P1": package()}})

        with self.assertRaisesRegex(ValueError, r"RouteID_target.*unknown stops.*'Z'"):
            self.load()

    def test_missing_route_does_not_match_nested_keys_or_values(self):
        decoy = {self.route_id + "_suffix": {self.route_id: [self.route_id, "}, NaN, {"]}}
        self.path.write_text(json.dumps(decoy), encoding="utf-8")

        with self.assertRaisesRegex(KeyError, r"RouteID_target.*package_data\.json"):
            self.load()

    def test_chunk_boundaries_in_escaped_keys_strings_and_nan(self):
        route_id = self.route_id + ' "Ω\\'
        decoy = {route_id: [route_id, 'escape \\" }, { ', "x" * 2000]}
        data = {"RouteID_decoy": decoy, route_id: {
            "A": {"P1": package(window=window(float("nan"), float("nan")))},
        }}
        self.path.write_text(" \n\t" + json.dumps(data), encoding="utf-8")

        for size in (1, 7, 64):
            with self.subTest(block_size=size), patch("src.package_loader._BLOCK_SIZE", size):
                result = load_route_packages(route_id, self.data_dir, self.stop_ids)
                self.assertEqual(result["route_id"], route_id)
                self.assertEqual(result["stops"]["A"]["package_count"], 1)

    def test_stops_reading_after_the_target_member(self):
        data = {self.route_id: {"A": {"P1": package()}}, "RouteID_after": {"padding": "x" * 4096}}
        source = StringIO(json.dumps(data))
        with (
            patch("src.package_loader._BLOCK_SIZE", 512),
            patch.object(Path, "open", return_value=source),
            patch.object(source, "read", wraps=source.read) as read,
        ):
            self.assertEqual(self.load()["stops"]["A"]["package_count"], 1)
            read.assert_called_once_with(512)

    def test_reports_truncated_json(self):
        self.path.write_text('{"RouteID_target": {"A": {', encoding="utf-8")

        with self.assertRaisesRegex(ValueError, r"RouteID_target.*Incomplete top-level JSON object"):
            self.load()
