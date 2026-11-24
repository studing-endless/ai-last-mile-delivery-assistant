"""Verify main-entry mode selection and the read-only D1 dashboard."""

from copy import deepcopy
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import app
from evals.validate_demo_data import load_package
from src import demo_dashboard, demo_embed, demo_portfolio


class DemoModeTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(app, "DATA_DIR", self.directory / "raw"))
        self.enterContext(patch.object(app, "DB_PATH", self.directory / "absent.sqlite3"))
        self.embed = self.enterContext(patch.object(app, "show_portfolio", return_value=False))
        self.blocked = []
        for name in ("save_run", "submit_review", "list_runs", "get_run", "get_review", "answer_question",
                     "setup_message", "optimize_route", "load_delivery_task", "evaluate_schedule"):
            self.blocked.append(self.enterContext(patch.object(app, name, side_effect=AssertionError(name))))
        self.package = load_package()

    def start(self):
        page = AppTest.from_string("import app\napp.main()", default_timeout=10).run()
        self.assertFalse(page.exception)
        return page

    @staticmethod
    def navigate(page, section):
        return page.radio(key="portfolio_nav").set_value(section).run()

    def test_read_only_app_mounts_the_v3_component(self):
        with (patch.object(app, "show_portfolio", demo_embed.show_portfolio),
              patch.object(demo_dashboard, "show_demo") as fallback):
            page = self.start()
        self.assertEqual(len(page.get("bidi_component")), 1)
        self.assertFalse(page.radio or page.button or page.chat_input)
        fallback.assert_not_called()
        for blocked in self.blocked:
            blocked.assert_not_called()

    def test_v3_build_uses_the_published_snapshot_and_assets(self):
        page = demo_embed.build_document()
        data = re.search(r'<script id="portfolio-data" type="application/json">(.*?)</script>', page)
        self.assertIsNotNone(data)
        self.assertEqual(json.loads(data[1]), json.loads((demo_embed.DOCS / "assets/data/portfolio.json").read_bytes()))
        for asset in ("assets/css/portfolio.css", "assets/js/portfolio.js"):
            self.assertIn((demo_embed.DOCS / asset).read_text(encoding="utf-8"), page)
            self.assertNotIn(f'"./{asset}"', page)
        self.assertNotIn('href="https://ai-last-mile-assistant.streamlit.app"', page)
        self.assertIn("05 / SYSTEM", page)

    def test_v3_invalid_bundle_leaves_the_d4_navigation_available(self):
        with (patch.object(app, "show_portfolio", demo_embed.show_portfolio),
              patch.object(demo_embed, "build_document", side_effect=ValueError("invalid snapshot"))):
            page = self.start()
        self.assertFalse(page.get("bidi_component"))
        self.assertEqual(tuple(page.radio[0].options), demo_portfolio.PAGES)

    def test_partial_or_missing_data_uses_demo_and_all_cases_match_d1(self):
        app.DATA_DIR.mkdir()
        for filename in app.DATA_FILES[:-1]:
            (app.DATA_DIR / filename).touch()
        page = self.start()
        self.assertTrue(any("在线只读演示模式" in item.value for item in page.get("html")))
        self.assertEqual(tuple(page.radio[0].options), demo_portfolio.PAGES)
        self.navigate(page, "配送方案分析")
        for case in self.package:
            page.radio[1].set_value(case["demo_case_id"]).run()
            self.assertFalse(page.exception)
            metrics = {metric.label: metric for metric in page.metric}
            self.assertTrue(any(f"{case['task']['stop_count']} 个站点" in item.value
                                and f"{case['task']['package_count']} 条包裹记录" in item.value for item in page.caption))
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

    def test_portfolio_overview_and_evaluation_use_published_metrics(self):
        page = self.start()
        self.assertEqual([metric.value for metric in page.metric], ["6,112 条", "12/12", "10.89%", "2.77%"])
        self.navigate(page, "项目评估")
        self.assertFalse(page.exception)
        self.assertEqual([metric.value for metric in page.metric], ["12/12", "12/12", "10/12", "10.89%", "2.77%", "6/12"])
        self.assertIn("5 个包裹、3 个站点", page.warning[0].value)
        scores = "\n".join(item.value for item in page.markdown)
        for score in ("3/4", "4/4", "2/4"):
            self.assertIn(score, scores)

    def test_ai_examples_distinguish_model_calls_from_application_checks(self):
        page = self.navigate(self.start(), "AI运营助手")
        self.assertIn("非实时AI对话", page.info[0].value)
        self.assertIn("8423 秒", page.success[0].value)
        self.assertEqual(len(page.json), 2)

        page.radio(key="ai_case").set_value("A02").run()
        self.assertFalse(page.exception)
        self.assertIn("5.3 秒", page.text[1].value)
        self.assertTrue(any("模型本轮未调用工具" in item.value for item in page.caption))
        self.assertTrue(any("应用补查" in item.value for item in page.markdown))
        self.assertIn("1347.7 秒", page.success[1].value)

        page.radio(key="ai_case").set_value("A03").run()
        self.assertFalse(page.exception)
        self.assertIn("审批需由人工", page.success[0].value)
        self.assertIn("12940.9 秒", page.success[0].value)
        self.assertFalse(page.chat_input)
        self.assertFalse(page.button)
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
        self.embed.assert_not_called()
        self.assertTrue(any(item.value == "本地完整模式" for item in page.caption))

    def test_missing_metrics_display_unavailable_and_omit_incomplete_chart(self):
        case = deepcopy(self.package[0])
        case["suggested"]["planned_service_time_seconds_sum"] = None
        case["comparisons"]["planned_service_time_seconds_sum"].update(suggested=None, delta=None, change_percent=None)
        case["suggested"]["risk_package_count"] = None
        case["comparisons"]["risk_package_count"].update(suggested=None, delta=None, change_percent=None)
        with patch.object(demo_dashboard, "load_package", return_value=[case]):
            page = self.navigate(self.start(), "配送方案分析")
        metrics = {metric.label: metric for metric in page.metric}
        self.assertEqual(metrics["模拟窗口风险包裹数（个）"].value, "未提供")
        self.assertFalse(page.get("vega_lite_chart"))
        self.assertTrue(any("部分时间指标未提供" in item.value for item in page.info))

    def test_invalid_package_shows_friendly_error(self):
        with patch.object(demo_dashboard, "load_package", side_effect=ValueError("fixture mismatch")):
            page = self.navigate(self.start(), "配送方案分析")
        self.assertTrue(any("校验失败" in item.value for item in page.error))
        self.assertEqual(len(page.radio), 1)

    def test_clean_copy_runs_without_raw_data_database_or_credentials(self):
        root = Path(app.__file__).resolve().parent
        target = self.directory / "clean"
        target.mkdir()
        shutil.copyfile(root / "app.py", target / "app.py")
        shutil.copytree(root / "src", target / "src", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(root / "demo_data", target / "demo_data")
        (target / "evals").mkdir()
        shutil.copyfile(root / "evals/validate_demo_data.py", target / "evals/validate_demo_data.py")
        shutil.copyfile(root / "evals/portfolio_evidence.json", target / "evals/portfolio_evidence.json")
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
    for section in ('AI运营助手', '项目评估', '配送方案分析'):
        page.radio(key='portfolio_nav').set_value(section).run()
        assert not page.exception, page.exception
    for selected in ('A', 'B', 'C'):
        page.radio[1].set_value(selected).run()
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
