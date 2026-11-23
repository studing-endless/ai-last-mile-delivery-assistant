"""Verify main-entry mode selection and the read-only D1 dashboard."""

from copy import deepcopy
from pathlib import Path
import os
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import app
from evals.validate_demo_data import load_package
from src import demo_dashboard


class DemoModeTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(app, "DATA_DIR", self.directory / "raw"))
        self.enterContext(patch.object(app, "DB_PATH", self.directory / "absent.sqlite3"))
        self.blocked = []
        for name in ("save_run", "submit_review", "list_runs", "get_run", "get_review", "answer_question",
                     "setup_message", "optimize_route", "load_delivery_task", "evaluate_schedule"):
            self.blocked.append(self.enterContext(patch.object(app, name, side_effect=AssertionError(name))))
        self.package = load_package()

    def start(self):
        page = AppTest.from_string("import app\napp.main()", default_timeout=10).run()
        self.assertFalse(page.exception)
        return page

    def test_partial_or_missing_data_uses_demo_and_all_cases_match_d1(self):
        app.DATA_DIR.mkdir()
        for filename in app.DATA_FILES[:-1]:
            (app.DATA_DIR / filename).touch()
        page = self.start()
        self.assertTrue(any(item.value == "在线只读演示模式" for item in page.caption))
        for case in self.package:
            page.radio[0].set_value(case["demo_case_id"]).run()
            self.assertFalse(page.exception)
            metrics = {metric.label: metric for metric in page.metric}
            self.assertEqual(metrics["站点数（含配送站）"].value, str(case["task"]["stop_count"]))
            self.assertEqual(metrics["包裹记录数"].value, str(case["task"]["package_count"]))
            total = case["suggested"]["total_operation_seconds"]
            self.assertEqual(metrics["预计总作业时间（秒）"].value, f"{total:,.1f}")
            delta = case["comparisons"]["total_operation_seconds"]["delta"]
            self.assertEqual(metrics["预计总作业时间（秒）"].delta, f"{delta:+,.1f} 秒")
            self.assertTrue(any(case["route_id"] in item.value for item in page.text))
            if case["demo_case_id"] == "B":
                self.assertTrue(any("增加 224.7 秒" in item.value for item in page.warning))
                self.assertTrue(any("历史快照审核状态 · REJECTED" in item.value for item in page.info))
                self.assertTrue(any("新增模拟风险包裹数 · 未提供" in item.value for item in page.caption))
            if case["demo_case_id"] == "C":
                self.assertTrue(any("2 个包裹，1 个站点" in item.value for item in page.warning))
            self.assertFalse(page.chat_input)
            self.assertFalse(page.button)
            self.assertFalse(page.get("pydeck_chart"))
        self.assertFalse(app.DB_PATH.exists())
        for blocked in self.blocked:
            blocked.assert_not_called()

    def test_all_four_files_select_local_workbench(self):
        app.DATA_DIR.mkdir()
        for filename in app.DATA_FILES:
            (app.DATA_DIR / filename).touch()
        with (patch.object(app, "_show_workbench") as workbench,
              patch.object(app, "_show_history", return_value=None) as history,
              patch.object(app, "_show_ai_assistant") as assistant,
              patch.object(demo_dashboard, "show_demo") as demo):
            page = self.start()
        workbench.assert_called_once()
        history.assert_called_once()
        assistant.assert_called_once_with(None)
        demo.assert_not_called()
        self.assertTrue(any(item.value == "本地完整模式" for item in page.caption))

    def test_missing_metrics_display_unavailable_and_omit_incomplete_chart(self):
        case = deepcopy(self.package[0])
        case["suggested"]["planned_service_time_seconds_sum"] = None
        case["comparisons"]["planned_service_time_seconds_sum"].update(suggested=None, delta=None, change_percent=None)
        case["suggested"]["risk_package_count"] = None
        case["comparisons"]["risk_package_count"].update(suggested=None, delta=None, change_percent=None)
        with patch.object(demo_dashboard, "load_package", return_value=[case]):
            page = self.start()
        metrics = {metric.label: metric for metric in page.metric}
        self.assertEqual(metrics["模拟窗口风险包裹数（个）"].value, "未提供")
        self.assertFalse(page.get("vega_lite_chart"))
        self.assertTrue(any("部分时间指标未提供" in item.value for item in page.info))

    def test_invalid_package_shows_friendly_error(self):
        with patch.object(demo_dashboard, "load_package", side_effect=ValueError("fixture mismatch")):
            page = self.start()
        self.assertTrue(any("校验失败" in item.value for item in page.error))
        self.assertFalse(page.radio)

    def test_clean_copy_runs_without_raw_data_database_or_credentials(self):
        root = Path(app.__file__).resolve().parent
        target = self.directory / "clean"
        target.mkdir()
        shutil.copyfile(root / "app.py", target / "app.py")
        shutil.copytree(root / "src", target / "src", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(root / "demo_data", target / "demo_data")
        (target / "evals").mkdir()
        shutil.copyfile(root / "evals/validate_demo_data.py", target / "evals/validate_demo_data.py")
        environment = {name: value for name, value in os.environ.items()
                       if not name.startswith(("ZHIPU_", "OPENAI_")) and name != "PYTHONPATH"}
        script = """
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch
from streamlit.testing.v1 import AppTest
import app
before = {str(p): sha256(p.read_bytes()).hexdigest() for p in Path('demo_data').rglob('*') if p.is_file()}
with patch('sqlite3.connect', side_effect=AssertionError('SQLite forbidden')), patch('openai.OpenAI', side_effect=AssertionError('API forbidden')):
    page = AppTest.from_string('import app\\napp.main()', default_timeout=10).run()
    assert not page.exception, page.exception
    for selected in ('A', 'B', 'C'):
        page.radio[0].set_value(selected).run()
        assert not page.exception, page.exception
    assert not page.chat_input and not page.button
assert not Path('outputs').exists() and not Path('data').exists() and not Path('.env').exists()
assert before == {name: sha256(Path(name).read_bytes()).hexdigest() for name in before}
print('CLEAN_DEMO_OK')
"""
        result = subprocess.run([sys.executable, "-B", "-c", script], cwd=target, env=environment,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLEAN_DEMO_OK", result.stdout)
