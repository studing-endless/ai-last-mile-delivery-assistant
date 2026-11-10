import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from evals import evaluate_ai as v1
from evals import evaluate_ai_v2 as v2


RUN_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ROUTE_ID = "RouteID_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def option(name, query, outcome="ok"):
    return {"name": name, "query": query, "outcome": outcome}


def call(name, query, error=None):
    return {"name": name, "query": query, "ok": error is None, "error_code": error}


class ToolGradingTests(unittest.TestCase):
    def test_right_run_on_unaccepted_tool_only_fails_tool_selection(self):
        query = {"run_id": RUN_ID}
        options = [option("compare_planning_run", query)]
        audit = [call("get_planning_run", query)]
        previous = v1.check_tools(options, audit)
        self.assertFalse(previous["business_arguments_correct"])
        current = v2.check_tools(options, audit)
        self.assertFalse(current["necessary_tool_selected"])
        self.assertTrue(current["business_arguments_correct"])
        self.assertIsNone(current["expected_tool_outcome"])
        self.assertEqual(current["wrong_argument_call_indices"], [])

    def test_right_tool_wrong_run_does_not_also_fail_outcome(self):
        options = [option("get_planning_run", {"run_id": RUN_ID})]
        current = v2.check_tools(options, [call("get_planning_run", {"run_id": "different"})])
        self.assertTrue(current["necessary_tool_selected"])
        self.assertFalse(current["business_arguments_correct"])
        self.assertEqual(current["wrong_argument_call_indices"], [1])
        self.assertIsNone(current["expected_tool_outcome"])

    def test_expected_failure_and_alternative_tool_paths(self):
        query = {"run_id": RUN_ID}
        names = ("get_planning_run", "compare_planning_run")
        options = [option(name, query, "run_not_found") for name in names]
        current = v2.check_tools(options, [call(names[1], query, "run_not_found")])
        self.assertTrue(current["necessary_tool_selected"])
        self.assertTrue(current["business_arguments_correct"])
        self.assertTrue(current["expected_tool_outcome"])
        wrong_outcome = v2.check_tools(options, [call(names[1], query)])
        self.assertFalse(wrong_outcome["expected_tool_outcome"])

    def test_wrong_or_hidden_route_id_and_no_comparable_parameter(self):
        options = [option("list_planning_runs", {"route_id": ROUTE_ID})]
        self.assertIsNone(v2.check_tools(options, [])["business_arguments_correct"])
        unrelated = v2.check_tools(options, [call("get_planning_run", {"run_id": RUN_ID})])
        self.assertIsNone(unrelated["business_arguments_correct"])
        hidden = v2.check_tools(options, [call("list_planning_runs", {"route_id": v2.HIDDEN_ID})])
        self.assertIsNone(hidden["business_arguments_correct"])
        self.assertEqual(hidden["unverified_argument_call_indices"], [1])
        malformed = v2.check_tools(options, [call("list_planning_runs", None, "invalid_arguments")])
        self.assertFalse(malformed["business_arguments_correct"])
        wrong = v2.check_tools(options, [call("list_planning_runs", {"route_id": "different"})])
        self.assertFalse(wrong["business_arguments_correct"])

    def test_redundant_and_wrong_queries_stay_visible(self):
        query = {"run_id": RUN_ID}
        options = [option("get_planning_run", query)]
        audit = [
            call("list_planning_runs", {"route_id": ROUTE_ID}),
            call("get_planning_run", query),
            call("compare_planning_run", {"run_id": "different"}),
        ]
        current = v2.check_tools(options, audit)
        self.assertTrue(current["expected_tool_outcome"])
        self.assertFalse(current["business_arguments_correct"])
        self.assertEqual(current["wrong_argument_call_indices"], [3])
        self.assertEqual(current["redundant_call_indices"], [1, 3])
        self.assertTrue(v2.check_tools([], [])["necessary_tool_selected"])
        self.assertFalse(v2.check_tools([], audit)["necessary_tool_selected"])


class NumericGradingTests(unittest.TestCase):
    def test_two_comparison_orders_and_multiple_metrics(self):
        examples = [
            ("建议路线的总作业时间比历史路线长224.7秒。", "总作业时间差值", 224.7),
            ("历史路线的总作业时间比建议路线短224.7秒。", "总作业时间差值", 224.7),
            ("建议路线比历史路线的行驶时间短18.72分钟。", "行驶时间差值", -1123),
            ("建议路线比历史路线多等了1,347.7秒。", "等待时间差值", 1347.7),
            ("建议路线的等待时间比历史路线的等待时间长1,347.7秒。", "等待时间差值", 1347.7),
        ]
        for answer, label, expected in examples:
            with self.subTest(answer=answer):
                result = v2.check_number(answer, label, {"unit": "seconds", "expected": expected})
                self.assertEqual(result["status"], "match")
                self.assertEqual(result["claims"][0]["rule"], "route_comparison")

    def test_explicit_wrong_sign_magnitude_and_conflicting_claims_fail(self):
        spec = {"unit": "seconds", "expected": 224.7}
        for answer in (
            "历史路线的总作业时间比建议路线长224.7秒。",
            "建议路线的总作业时间比历史路线长-224.7秒。",
            "建议路线的总作业时间比历史路线长225秒。",
            "总作业时间差值：224.7秒。历史路线的总作业时间比建议路线长224.7秒。",
        ):
            with self.subTest(answer=answer):
                self.assertEqual(v2.check_number(answer, "总作业时间差值", spec)["status"], "mismatch")

    def test_ambiguous_conditional_and_unrelated_comparisons_remain_unverified(self):
        spec = {"unit": "seconds", "expected": 224.7}
        for answer in (
            "比历史路线长224.7秒。",
            "建议路线的总作业时间比历史路线长224.7。",
            "建议路线的总作业时间比历史路线长224.7至300秒。",
            "建议路线的总作业时间比历史路线长224.7秒，或者300秒。",
            "建议路线的等待时间比历史路线长224.7秒。",
            "建议路线的总作业时间比历史路线的等待时间长224.7秒。",
            "如果重新优化，建议路线的总作业时间比历史路线长224.7秒。",
            "建议路线的总作业时间比历史路线长224.7秒吗？",
            "并不是说，建议路线的总作业时间比历史路线长224.7秒。",
            "建议路线的总作业时间比建议路线长224.7秒。",
        ):
            with self.subTest(answer=answer):
                self.assertEqual(v2.check_number(answer, "总作业时间差值", spec)["status"], "unverified")

    def test_direct_counts_absolute_times_and_rounding(self):
        self.assertEqual(v2.check_number("规划记录数：11 条。", "规划记录数",
                                        {"unit": "records", "expected": 11})["status"], "match")
        self.assertEqual(v2.check_number("规划记录数：10.99 条。", "规划记录数",
                                        {"unit": "records", "expected": 11})["status"], "mismatch")
        self.assertEqual(v2.check_number("建议路线预计总作业时间为 60.03 秒。", "建议路线预计总作业时间",
                                        {"unit": "seconds", "expected": 60})["status"], "match")


class ReplayTests(unittest.TestCase):
    def test_replay_preserves_original_trace_and_requires_matching_case_set(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            case_file, report_path = root / "cases.json", root / "saved.json"
            turn = {"question": "请比较建议路线和历史路线。", "tool_options": [option("compare_planning_run", {"run_id": RUN_ID})]}
            suite = {"version": "test", "cases": [{"case_id": "TEST", "turns": [turn]}]}
            case_file.write_text(v1.canonical_json(suite), encoding="utf-8")
            spec = {"unit": "seconds", "expected": 1}
            reference = {"numbers": {"等待时间差值": spec}}
            audit = [call("get_planning_run", {"run_id": RUN_ID})]
            original = {
                "question": turn["question"], "status": "automatic_failure", "tool_calls": v1.safe_audit(audit),
                "answer": "建议路线比历史路线多等1秒。", "reference": reference,
                "number_checks": [v1.check_number("建议路线比历史路线多等1秒。", "等待时间差值", spec)],
                "tool_checks": {**v1.check_tools(turn["tool_options"], audit), "assessable": True},
                "manual_review": {"status": "pending"}, "api_error": None,
                "read_only": {"attempted_unlisted_tool": False},
            }
            report = {
                "case_set_version": "test", "case_set_sha256": hashlib.sha256(case_file.read_bytes()).hexdigest(),
                "reference_sha256": hashlib.sha256(v1.canonical_json({"TEST": [reference]}).encode("utf-8")).hexdigest(),
                "eval_version": "1.0", "mode": "live", "model": "saved-model", "case_ids": ["TEST"],
                "api_requests": 2, "external_requests": 2, "results": [{"case_id": "TEST", "turns": [original]}],
            }
            saved = v1.canonical_json(report).encode("utf-8")
            report_path.write_bytes(saved)
            with (
                patch.object(v1, "CASE_FILE", case_file),
                patch.object(v1, "REPORT_DIR", root),
                patch.object(v1, "setup_message", side_effect=AssertionError("replay must be offline")),
                patch.object(v1, "frozen_database", side_effect=AssertionError("replay must not access SQLite")),
            ):
                self.assertEqual(v2.main(["--replay", str(report_path)]), 0)
                case_file.write_text(v1.canonical_json({**suite, "version": "changed"}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    v2.replay_report(report_path)
            self.assertEqual(report_path.read_bytes(), saved)
            output, = root.glob("replay-v2.0-*.json")
            replayed = json.loads(output.read_bytes())
            self.assertEqual(replayed["external_requests"], 0)
            self.assertEqual(replayed["source_report"]["external_requests"], 2)
            item = replayed["results"][0]["turns"][0]
            self.assertEqual(item["status"], "automatic_failure")
            self.assertEqual(item["original_assessment"]["number_checks"][0]["status"], "unverified")
            self.assertEqual(item["number_checks"][0]["status"], "match")
            self.assertEqual(item["manual_review"]["status"], "pending")
