import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from evals import evaluate_ai as evaluation
from src.planning_db import save_run, submit_review


RUN_ID = "11111111-1111-4111-8111-111111111111"
MISSING_ID = "00000000-0000-4000-8000-000000000000"
ROUTE_ID = "RouteID_22222222-2222-4222-8222-222222222222"


def tool(name, run_id=RUN_ID, outcome="ok"):
    return {"name": name, "query": {"run_id": run_id}, "outcome": outcome}


def tool_call(name, arguments, call_id):
    return SimpleNamespace(
        type="function", id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def response(*calls, answer=None):
    message = SimpleNamespace(content=answer, tool_calls=list(calls))
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="tool_calls" if calls else "stop")])


class AIEvaluationTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = directory / "planning.sqlite3"
        self.reports = directory / "reports"
        risk = {"risk_package_count": 0, "risk_stop_count": 0, "window_conflict_stop_count": 0}
        save_run(self.path, {
            "run_id": RUN_ID, "route_id": ROUTE_ID, "started_at_utc": "2026-11-09T12:00:00+00:00",
            "solver_status": "ROUTING_SUCCESS", "solve_seconds": 20, "search_budget_seconds": 20,
            "algorithm": "test-only", "parameters": {}, "suggested_order": ["VE", "A", "VE"],
            "historical_metrics": {"driving_seconds": 80, "waiting_seconds": 0, "total_operation_seconds": 100, **risk},
            "suggested_metrics": {"driving_seconds": 70, "waiting_seconds": 15, "total_operation_seconds": 105, **risk},
            "error_message": None,
        })
        submit_review(self.path, RUN_ID, "REJECTED", "人工测试备注")
        self.compare = tool("compare_planning_run")
        self.get = tool("get_planning_run")
        self.turn = {
            "question": f"运行 {RUN_ID} 的总作业时间变化是多少？",
            "tool_options": [self.compare, self.get], "reference": self.compare,
            "numbers": {"总作业时间差值": {"unit": "seconds", "path": "data.metrics.total_operation_seconds.delta"}},
            "review": "说明离线模拟和人工审核边界。",
            "mock_answer": "这是离线模拟估计。",
        }
        self.case = {"case_id": "TEST", "category": "测试", "turns": [self.turn]}

    def prepare(self, case=None):
        case = case or self.case
        return evaluation.prepare_references([case], self.path)[case["case_id"]]

    def test_cli_writes_offline_report_without_loading_credentials_or_writing_database(self):
        suite_path = self.reports.parent / "cases.json"
        suite_path.write_text(json.dumps({"version": "test", "cases": [self.case]}), encoding="utf-8")
        before = self.path.read_bytes()
        with (
            patch.object(evaluation, "CASE_FILE", suite_path),
            patch.object(evaluation, "DEFAULT_DB_PATH", self.path),
            patch.object(evaluation, "REPORT_DIR", self.reports),
            patch.object(evaluation, "setup_message", side_effect=AssertionError("offline must not load credentials")),
            patch("openai.OpenAI", side_effect=AssertionError("offline must not construct a real client")),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(evaluation.main([]), 0)
        (report_path,) = self.reports.glob("offline_mock-*.json")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual((report["offline_cases_passed"], report["external_requests"], report["api_requests"]), (1, 0, 2))
        turn = report["results"][0]["turns"][0]
        self.assertEqual(turn["number_checks"][0]["status"], "match")
        self.assertEqual(turn["manual_review"]["status"], "pending")
        self.assertEqual(self.path.read_bytes(), before)

    def test_alternative_tool_is_accepted_and_extra_call_is_flagged(self):
        replies = iter([
            response(tool_call("get_planning_run", {"run_id": RUN_ID}, "first"),
                     tool_call("compare_planning_run", {"run_id": RUN_ID}, "extra")),
            response(answer="预计总作业时间增加了 5 秒。"),
        ])
        requests = []

        def create(**kwargs):
            requests.append(kwargs)
            return next(replies)

        entry, _ = evaluation.evaluate_turn(self.turn, self.prepare()[0], [], evaluation.CountedClient(create, 3), "test", self.path)
        checks = entry["tool_checks"]
        self.assertTrue(checks["necessary_tool_selected"])
        self.assertTrue(checks["business_arguments_correct"])
        self.assertTrue(checks["expected_tool_outcome"])
        self.assertEqual(checks["redundant_call_indices"], [2])
        self.assertEqual(entry["number_checks"][0]["status"], "match")
        self.assertEqual([item["tool_call_id"] for item in requests[1]["messages"] if item["role"] == "tool"], ["first", "extra"])

    def test_malformed_arguments_and_unlisted_tool_are_audited_and_do_not_write(self):
        before = self.path.read_bytes()
        replies = iter([
            response(tool_call("compare_planning_run", {"run_id": 123}, "wrong-type"),
                     tool_call("submit_review", {"run_id": RUN_ID}, "blocked")),
            response(answer="Authorization: Bearer not-for-logs\n测试密钥是 eval-private-key。"),
        ])
        client = evaluation.CountedClient(lambda **_: next(replies), 3)
        with patch.dict(os.environ, {"ZHIPU_API_KEY": "eval-private-key"}):
            entry, _ = evaluation.evaluate_turn(self.turn, self.prepare()[0], [], client, "test", self.path)
        self.assertEqual(entry["status"], "automatic_failure")
        self.assertTrue(entry["read_only"]["attempted_unlisted_tool"])
        self.assertFalse(entry["tool_checks"]["business_arguments_correct"])
        self.assertEqual(entry["tool_checks"]["wrong_argument_call_indices"], [1])
        self.assertEqual([item["error_code"] for item in entry["tool_calls"]], ["invalid_arguments", "unknown_tool"])
        self.assertIsNone(entry["tool_calls"][0]["arguments"])
        for secret in ("Authorization", "not-for-logs", "eval-private-key"):
            self.assertNotIn(secret, json.dumps(entry))
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_run_is_a_correct_tool_outcome(self):
        missing = tool("get_planning_run", MISSING_ID, "run_not_found")
        turn = {
            "question": f"运行 {MISSING_ID} 存在吗？", "tool_options": [missing], "reference": missing,
            "review": "说明未找到。", "mock_answer": "未找到该运行。",
        }
        reference, = self.prepare({**self.case, "turns": [turn]})
        entry, _ = evaluation.evaluate_turn(turn, reference, [], evaluation.mock_client(turn, reference), "test", self.path)
        self.assertFalse(entry["tool_calls"][0]["ok"])
        self.assertTrue(entry["tool_checks"]["expected_tool_outcome"])
        self.assertEqual(entry["status"], "needs_review")

    def test_numeric_units_sign_rounding_and_unmatched_language(self):
        spec = {"unit": "seconds", "expected": -1123.0}
        label = "行驶时间差值"
        examples = (
            ("行驶时间减少了 18.72 分钟。", "match"),
            ("行驶时间差值：-1,123.04 秒。", "match"),
            ("行驶时间差值：-1123.06 秒。", "mismatch"),
            ("行驶时间减少 +1,123.0 秒。", "mismatch"),
            ("行驶时间差值：1123 秒。", "mismatch"),
            ("行驶时间差值：-1123 秒。行驶时间增加了1123秒。", "mismatch"),
            ("总作业时间是1123秒。", "unverified"),
            ("行驶时间差值：-1123。", "unverified"),
        )
        for answer, expected in examples:
            with self.subTest(answer=answer):
                self.assertEqual(evaluation.check_number(answer, label, spec)["status"], expected)
        count = {"unit": "records", "expected": 11}
        self.assertEqual(evaluation.check_number("规划记录数：10.99 条。", "规划记录数", count)["status"], "mismatch")

    def test_multi_turn_uses_history_but_queries_again(self):
        followup = {**self.turn, "question": "那这次运行呢？"}
        case = {**self.case, "turns": [self.turn, followup]}
        replies = iter([
            response(tool_call("compare_planning_run", {"run_id": RUN_ID}, "c1")),
            response(answer="总作业时间差值：5 秒。"),
            response(tool_call("get_planning_run", {"run_id": RUN_ID}, "c2")),
            response(answer="总作业时间增加了5秒。"),
        ])
        requests = []

        def create(**kwargs):
            requests.append(kwargs)
            return next(replies)

        result, = evaluation.run_suite(
            [case], {"TEST": self.prepare(case)}, self.path, model="test", live_client=evaluation.CountedClient(create, 10),
        )
        self.assertEqual(result["api_requests"], 4)
        self.assertIn({"role": "user", "content": self.turn["question"]}, requests[2]["messages"])
        self.assertIn({"role": "assistant", "content": "总作业时间差值：5 秒。"}, requests[2]["messages"])
        self.assertEqual(result["turns"][1]["tool_calls"][0]["arguments"], {"run_id": RUN_ID})

    def test_rate_limit_is_service_failure_and_no_subsequent_requests_are_made(self):
        calls = []

        def throttled(**kwargs):
            calls.append(kwargs)
            error = RuntimeError("Authorization: test-never-log")
            error.status_code = 429
            error.body = {"error": {"code": "1305", "message": "unsafe response body"}}
            raise error

        later = {**self.case, "case_id": "LATER"}
        cases = [self.case, later]
        results = evaluation.run_suite(
            cases, evaluation.prepare_references(cases, self.path), self.path, model="test",
            live_client=evaluation.CountedClient(throttled, 10),
        )
        first, second = (result["turns"][0] for result in results)
        self.assertEqual(first["status"], "service_failure")
        self.assertEqual(first["api_error"], {"code": "api_error", "http_status": 429, "provider_code": "1305"})
        self.assertFalse(first["tool_checks"]["assessable"])
        self.assertEqual(second["status"], "not_run_after_service_failure")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("test-never-log", json.dumps(results))

    def test_budget_refuses_real_mode_without_explicit_cases_or_sufficient_capacity(self):
        suite_path = self.reports.parent / "cases.json"
        two_turn_case = {**self.case, "turns": [self.turn, self.turn]}
        suite_path.write_text(json.dumps({"version": "test", "cases": [two_turn_case]}), encoding="utf-8")
        with (
            patch.object(evaluation, "CASE_FILE", suite_path),
            patch.object(evaluation, "frozen_database", side_effect=AssertionError("Invalid budgets must fail before evaluation")),
        ):
            for args in (["--live"], ["--live", "--case", two_turn_case["case_id"], "--max-requests", "9"]):
                with self.subTest(args=args):
                    stderr = io.StringIO()
                    with redirect_stderr(stderr), self.assertRaises(SystemExit) as failure:
                        evaluation.main(args)
                    self.assertEqual(failure.exception.code, 2)
                    self.assertIn("Live mode requires", stderr.getvalue())
        create = Mock(return_value="response")
        client = evaluation.CountedClient(create, 1)
        self.assertEqual(client.create(), "response")
        with self.assertRaises(RuntimeError):
            client.create()
        create.assert_called_once()

    def test_manual_entry_uses_configured_model_and_sdk_limits_with_a_mock(self):
        suite_path = self.reports.parent / "cases.json"
        suite_path.write_text(json.dumps({"version": "test", "cases": [self.case]}), encoding="utf-8")
        fake_sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(side_effect=[
            response(tool_call("compare_planning_run", {"run_id": RUN_ID}, "call-1")),
            response(answer="总作业时间差值：5 秒。"),
        ]))))
        config = {"ZHIPU_API_KEY": "fixture-only", "ZHIPU_BASE_URL": "https://example.invalid"}
        with (
            patch.dict(os.environ, config),
            patch.object(evaluation, "CASE_FILE", suite_path),
            patch.object(evaluation, "DEFAULT_DB_PATH", self.path),
            patch.object(evaluation, "REPORT_DIR", self.reports),
            patch.object(evaluation, "setup_message", return_value=None),
            patch.object(evaluation, "configured_model", return_value="glm-4-flash-250414"),
            patch("openai.OpenAI") as constructor,
            redirect_stdout(io.StringIO()),
        ):
            constructor.return_value.__enter__.return_value = fake_sdk
            self.assertEqual(evaluation.main(["--live", "--case", "TEST", "--max-requests", "5"]), 0)
        constructor.assert_called_once_with(
            api_key="fixture-only", base_url="https://example.invalid", timeout=20, max_retries=0,
        )
        self.assertEqual(fake_sdk.chat.completions.create.call_args.kwargs["model"], "glm-4-flash-250414")
        (report_path,) = self.reports.glob("live-*.json")
        contents = report_path.read_text(encoding="utf-8")
        self.assertNotIn("fixture-only", contents)
        self.assertNotIn("example.invalid", contents)
        report = json.loads(contents)
        self.assertEqual(report["mode"], "live")
        self.assertEqual(report["model"], "glm-4-flash-250414")
        self.assertIsNone(report["offline_cases_passed"])
        self.assertEqual(report["api_requests"], 2)
