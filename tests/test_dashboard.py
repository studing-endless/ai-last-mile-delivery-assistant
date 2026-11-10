"""Streamlit regression tests for chart data and planning snapshot labels."""

import json
import unittest
from datetime import UTC, datetime
from sqlite3 import OperationalError
from unittest.mock import patch

from pyarrow.ipc import open_stream
from streamlit.proto.Metric_pb2 import Metric
from streamlit.testing.v1 import AppTest

import app


class DashboardTests(unittest.TestCase):
    def setUp(self):
        departure = datetime(2020, 1, 1, tzinfo=UTC)
        self.task = {
            "route_id": app.DEFAULT_ROUTE_ID,
            "station_code": "DEPOT",
            "station_id": "VE",
            "delivery_date": departure.date(),
            "departure_datetime_utc": departure,
            "stop_count": 3,
            "stops": {
                "VE": {"type": "Station", "lat": 34.0, "lng": -118.0},
                "AA": {"type": "Dropoff", "lat": 34.1, "lng": -118.1},
                "BB": {"type": "Dropoff", "lat": 34.2, "lng": -118.2},
            },
            "ordered_stops": ["VE", "AA", "BB"],
            "packages_by_stop": {},
        }
        self.historical = {
            "driving_seconds": 60.125,
            "planned_service_time_seconds_sum": 180,
            "waiting_seconds": 0,
            "total_operation_seconds": 240.125,
            "risk_package_count": 0,
        }
        self.suggested = {
            **self.historical,
            "driving_seconds": 30.125,
            "waiting_seconds": 60,
            "total_operation_seconds": 270.125,
        }
        self.record = {
            "run_id": "session-run",
            "route_id": app.DEFAULT_ROUTE_ID,
            "started_at_utc": departure.isoformat(),
            "solver_status": "ROUTING_SUCCESS",
            "solve_seconds": 1,
            "search_budget_seconds": 20,
            "algorithm": "test solver",
            "parameters": {},
            "error_message": None,
            "suggested_order": ["VE", "BB", "AA", "VE"],
            "historical_metrics": self.historical,
            "suggested_metrics": self.suggested,
        }
        self.mock("_data_version", return_value=("fixture",))
        self.mock("_load_task", return_value=self.task)
        self.mock("_load_map_route", return_value=self.task)
        self.mock("_run_plan", return_value={"record": self.record})
        self.mock("list_runs", return_value=[])
        self.mock("save_run")
        self.mock("setup_message", return_value="模拟验收")
        self.mock("configured_model", return_value=None)

    def mock(self, name, **kwargs):
        return self.enterContext(patch.object(app, name, **kwargs))

    def start(self, *, solve=True):
        page = AppTest.from_string("import app\napp.main()").run()
        self.click(page, "加载配送任务")
        if solve:
            self.click(page, "生成建议路线")
        return page

    def click(self, page, label):
        next(button for button in page.button if button.label == label).click().run()
        self.assertFalse(page.exception)

    def chart_specs(self, page):
        return [json.loads(chart.proto.spec) for chart in page.get("vega_lite_chart")]

    def test_time_cards_use_neutral_zero_and_warn_when_total_increases(self):
        page = self.start()
        metrics = {metric.label: metric for metric in page.metric}
        zero = metrics["窗口风险包裹数"]
        self.assertEqual((zero.delta, zero.proto.color, zero.proto.direction), ("0 个", Metric.GRAY, Metric.NONE))
        driving = metrics["预计行驶时间（秒）"]
        self.assertEqual((driving.proto.color, driving.proto.direction), (Metric.GREEN, Metric.DOWN))
        total = metrics["预计总作业时间（秒）"]
        self.assertEqual((total.delta, total.proto.color, total.proto.direction), ("+30.0 秒 (+12.49%)", Metric.RED, Metric.UP))
        self.assertTrue(any("总作业时间增加" in item.value and "30.0 秒" in item.value for item in page.warning))
        self.assertEqual(metrics["历史预计总作业（分钟）"].value, "4.00")
        self.assertEqual(metrics["建议预计总作业（分钟）"].value, "4.50")
        self.assertEqual(metrics["总作业差值（建议 − 历史）（分钟）"].value, "+0.50")
        chart = self.chart_specs(page)[0]
        self.assertEqual(chart["encoding"]["x"]["stack"], "zero")
        values = open_stream(page.get("vega_lite_chart")[0].proto.data.data).read_all().to_pylist()
        self.assertEqual(len(values), 6)
        self.assertEqual({row["路线"] for row in values}, {"历史路线", "建议路线"})
        self.assertEqual(values[0]["原始秒数"], "60.125")

    def test_risk_increase_is_flagged_even_when_total_decreases(self):
        self.historical.update(risk_package_count=1, risk_stop_count=1)
        self.suggested.update(risk_package_count=3, risk_stop_count=2, window_conflict_stop_count=1,
                              waiting_seconds=0, total_operation_seconds=210.125)
        page = self.start()
        warning, = [item.value for item in page.warning if "模拟时间窗口风险增加" in item.value]
        self.assertIn("窗口风险包裹数由 1 增至 3", warning)
        self.assertIn("窗口风险站点数由 1 增至 2", warning)
        self.assertNotIn("窗口交集为空的站点数", warning)  # Missing historical count is unknown.
        self.assertFalse(any("总作业时间增加" in item.value for item in page.warning))
        self.assertTrue(any("可行建议路线" in item.value and "人工审核" in item.value for item in page.info))

    def test_route_modes_and_missing_coordinates_do_not_create_a_shortcut(self):
        self.task["stops"]["BB"]["lat"] = float("nan")
        page = self.start()
        self.assertTrue(any("1 个站点缺少有效坐标" in item.value for item in page.warning))
        route = page.radio(key="current_route_map")
        self.assertEqual(route.value, "同时显示")
        layers = self.chart_specs(page)[-1]["layer"]
        lines = layers[0]["data"]["values"]
        self.assertEqual(len(lines), 2)
        self.assertEqual({row["路线"] for row in lines}, {"历史路线", "建议路线"})
        points = layers[1]["data"]["values"]
        depot = next(point for point in points if point["站点 ID"] == "VE")
        self.assertEqual(depot["站点类型"], "配送站")
        self.assertEqual(depot["历史路线序号"], "0、3")
        self.assertEqual(depot["建议路线序号"], "0、3")
        route.set_value("历史路线").run()
        self.assertFalse(page.exception)
        only_historical = self.chart_specs(page)[-1]["layer"][0]["data"]["values"]
        self.assertEqual([row["路线"] for row in only_historical], ["历史路线"])

    def test_all_coordinates_missing_still_loads_the_task(self):
        for stop in self.task["stops"].values():
            stop.pop("lng")
        page = self.start(solve=False)
        self.assertTrue(any("缺少有效站点坐标" in item.value for item in page.warning))
        self.assertFalse(self.chart_specs(page))

    def test_current_unsaved_and_history_keep_distinct_sources_and_metrics(self):
        saved = {**self.record, "run_id": "saved-run", "suggested_metrics": self.historical}
        self.mock("list_runs", return_value=[saved])
        self.mock("get_run", return_value=saved)
        self.mock("get_review", return_value={"decision": "PENDING"})
        write = self.mock("save_run", side_effect=[OperationalError("test write failure"), True])
        page = self.start()
        current, history, _ = page.tabs
        self.assertTrue(any("当前会话方案（尚未保存）" in item.value for item in current.caption))
        page.selectbox[0].set_value("saved-run").run()
        self.assertFalse(page.exception)
        current, history, _ = page.tabs
        self.assertTrue(any("已保存方案 · run_id `saved-run`" in item.value for item in history.caption))
        self.assertTrue(any(metric.value == "270.1" for metric in current.metric))
        self.assertTrue(any(metric.value == "240.1" for metric in history.metric))
        self.click(page, "保存本次规划")
        self.assertTrue(any("已保存方案 · run_id `session-run`" in item.value for item in page.tabs[0].caption))
        self.assertTrue(any("已保存方案 · run_id `saved-run`" in item.value for item in page.tabs[1].caption))
        self.assertEqual(write.call_count, 2)
