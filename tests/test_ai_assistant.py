import json
import os
import tempfile
import unittest
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from src.ai_assistant import answer_question, configured_model, setup_message
from src.planning_db import get_run, list_runs, save_run, submit_review


class FakeClient:
    def __init__(self, *responses):
        self.pending = iter(responses)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **request):
        self.requests.append(request)
        response = next(self.pending)
        if isinstance(response, Exception):
            raise response
        return response


def call(name, arguments, call_id="call-1"):
    return SimpleNamespace(type="function", id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def response(*calls, text=None, reasoning=None):
    message = SimpleNamespace(content=text, tool_calls=list(calls), reasoning_content=reasoning)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="tool_calls" if calls else "stop")])


def api_failure(status, body, code=None):
    error = RuntimeError("raw exception fixture")
    error.status_code = status
    error.body = body
    error.code = code
    return error


def returned_tool_output(request, call_id):
    return next(
        json.loads(message["content"])
        for message in request["messages"]
        if message["role"] == "tool" and message["tool_call_id"] == call_id
    )


class AssistantTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "planning.sqlite3"
        risks = {"risk_package_count": 0, "risk_stop_count": 0, "window_conflict_stop_count": 0}
        save_run(self.path, {
            "run_id": "run-a", "route_id": "RouteID_test", "started_at_utc": "2026-11-09T12:00:00+00:00",
            "solver_status": "ROUTING_SUCCESS", "solve_seconds": 20, "search_budget_seconds": 20,
            "algorithm": "OR-Tools RoutingModel", "parameters": {}, "error_message": None,
            "suggested_order": ["VE", "A", "VE"],
            "historical_metrics": {"driving_seconds": 80, "waiting_seconds": 0, "total_operation_seconds": 100, **risks},
            "suggested_metrics": {"driving_seconds": 70, "waiting_seconds": 15, "total_operation_seconds": 105, **risks},
        })
        submit_review(self.path, "run-a", "REJECTED", "总作业时间增加")

    def ask(self, client, **kwargs):
        return answer_question("这次规划怎么样？", client=client, model="test-model", db_path=self.path, **kwargs)

    def test_function_round_trip_uses_saved_data_and_original_call_id(self):
        compare = call("compare_planning_run", '{"run_id":"run-a"}')
        client = FakeClient(response(compare, reasoning="mock reasoning"), response(text="总作业时间增加 5 秒。"))
        before = self.path.read_bytes()
        result = self.ask(client, selected_run_id="run-a")
        self.assertEqual(result["answer"], "总作业时间增加 5 秒。")
        self.assertEqual(result["tool_calls"][0]["query"], {"run_id": "run-a"})
        self.assertEqual(self.path.read_bytes(), before)

        first, followup = client.requests
        self.assertEqual(first["model"], "test-model")
        self.assertEqual(first["messages"][0]["role"], "system")
        self.assertIn("run-a", first["messages"][0]["content"])
        self.assertEqual(first["tool_choice"], "auto")
        self.assertTrue(all(tool["type"] == "function" for tool in first["tools"]))
        self.assertEqual({tool["function"]["name"] for tool in first["tools"]}, {
            "list_planning_runs", "get_planning_run", "compare_planning_run",
        })
        self.assertEqual(followup["messages"][2], {
            "role": "assistant", "content": None, "reasoning_content": "mock reasoning",
            "tool_calls": [{"id": "call-1", "type": "function", "function": {
                "name": "compare_planning_run", "arguments": '{"run_id":"run-a"}',
            }}],
        })
        data = returned_tool_output(followup, "call-1")["data"]
        self.assertEqual(data["metrics"]["total_operation_seconds"]["delta"], 5)
        self.assertEqual(data["review"]["decision"], "REJECTED")

    def test_handles_consecutive_calls_and_multiple_outputs(self):
        client = FakeClient(
            response(call("list_planning_runs", '{"route_id":"RouteID_test"}', "list")),
            response(call("get_planning_run", '{"run_id":"run-a"}', "get"),
                     call("compare_planning_run", '{"run_id":"run-a"}', "compare")),
            response(text="找到了运行记录。"),
        )
        result = self.ask(client)
        self.assertTrue(result["ok"])
        self.assertEqual([entry["call_id"] for entry in result["tool_calls"]], ["list", "get", "compare"])
        self.assertEqual(returned_tool_output(client.requests[1], "list")["data"]["runs"][0]["run_id"], "run-a")
        self.assertEqual(returned_tool_output(client.requests[2], "get")["data"]["suggested_order"], ["VE", "A", "VE"])

    def test_nonempty_list_and_two_details_correct_a_false_empty_answer(self):
        saved = get_run(self.path, "run-a", read_only=True)
        save_run(self.path, {**saved, "run_id": "run-b"})
        client = FakeClient(
            response(call("list_planning_runs", '{"route_id":"RouteID_test"}', "list")),
            response(call("get_planning_run", '{"run_id":"run-a"}', "first")),
            response(call("get_planning_run", '{"run_id":"run-b"}', "second")),
            response(text="未找到相关规划记录。"),
        )
        result = answer_question("查询 RouteID_test 的已保存规划记录", client=client, model="test-model", db_path=self.path)
        self.assertEqual(result["answer"], "Amazon Route ID `RouteID_test` 有 2 条已保存的离线模拟规划记录。")
        self.assertEqual(result["tool_calls"][0]["result_count"], 2)
        self.assertEqual(returned_tool_output(client.requests[1], "list")["data"]["record_count"], 2)
        for call_id, run_id in (("first", "run-a"), ("second", "run-b")):
            data = returned_tool_output(client.requests[-1], call_id)["data"]
            self.assertEqual(data["run_id"], run_id)
            self.assertEqual(data["suggested_metrics"]["total_operation_seconds"], 105)

    def test_empty_and_filtered_lookups_keep_the_model_answer(self):
        for question, route, count in (
            ("查询 RouteID_missing 的规划记录", "RouteID_missing", 0),
            ("查询 RouteID_test 已批准的规划记录", "RouteID_test", 1),
        ):
            with self.subTest(question=question):
                arguments = json.dumps({"route_id": route})
                client = FakeClient(response(call("list_planning_runs", arguments)), response(text="未找到相关规划记录。"))
                result = answer_question(question, client=client, model="test-model", db_path=self.path)
                self.assertEqual(result["answer"], "未找到相关规划记录。")
                self.assertEqual(result["tool_calls"][0]["result_count"], count)

    def test_no_tool_answer_can_use_conversation_history(self):
        client = FakeClient(response(text="可以查询已保存的规划。"))
        previous = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好"}]
        result = self.ask(client, history=previous)
        self.assertTrue(result["ok"])
        self.assertEqual(result["tool_calls"], [])
        self.assertEqual(client.requests[0]["messages"][1:3], previous)
        self.assertEqual(len(client.requests), 1)

    def test_waiting_followup_queries_again_and_replaces_another_metrics_delta(self):
        history = [
            {"role": "user", "content": "运行 run-a 的审核状态是什么？"},
            {"role": "assistant", "content": "已驳回。总作业时间比历史路线长 5 秒。"},
        ]
        client = FakeClient(response(text="这次运行的建议路线比历史路线多等了 5 秒。"))
        before = self.path.read_bytes()
        reply = answer_question(
            "那这次运行的建议路线比历史路线多等了多久？", history=history,
            client=client, model="test-model", db_path=self.path,
        )
        self.assertEqual(reply["run_id"], "run-a")
        self.assertIn("等待时间差值（建议减历史）：15.0 秒", reply["answer"])
        self.assertIn("离线模拟估计", reply["answer"])
        entry, = reply["tool_calls"]
        self.assertEqual(entry["name"], "compare_planning_run")
        self.assertEqual(entry["origin"], "evidence_fallback")
        evidence = entry["evidence"]
        self.assertEqual(evidence["source"]["run_id"], "run-a")
        self.assertEqual(evidence["metrics"]["total_operation_seconds"]["delta"], 5)
        self.assertEqual(evidence["metrics"]["waiting_seconds"]["delta"], 15)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_waiting_claim_is_checked_against_its_own_field_even_after_a_valid_lookup(self):
        client = FakeClient(response(call("compare_planning_run", '{"run_id":"run-a"}')),
                            response(text="等待时间增加了 5 秒。"))
        reply = answer_question("运行 run-a 的等待时间变化是多少？", client=client, model="test-model", db_path=self.path)
        self.assertIn("等待时间差值（建议减历史）：15.0 秒", reply["answer"])
        self.assertEqual(len(reply["tool_calls"]), 1)
        self.assertNotIn("origin", reply["tool_calls"][0])

    def test_changed_selection_uses_current_run_even_if_model_queries_old_run(self):
        run = get_run(self.path, "run-a", read_only=True)
        save_run(self.path, {**run, "run_id": "run-b", "suggested_metrics": {**run["suggested_metrics"], "waiting_seconds": 23}})
        client = FakeClient(response(call("get_planning_run", '{"run_id":"run-a"}')),
                            response(text="运行 run-a 的建议路线等待 15 秒。"))
        history = [{"role": "user", "content": "查一下 run-a。", "run_id": "run-a"},
                   {"role": "assistant", "content": "建议路线等待时间为 15 秒。"}]
        reply = answer_question(
            "这次的等待时间是多少？", selected_run_id="run-b", history=history,
            client=client, model="test-model", db_path=self.path,
        )
        self.assertEqual(reply["run_id"], "run-b")
        self.assertIn("建议路线等待时间：23 秒", reply["answer"])
        self.assertNotIn("run-a", reply["answer"])
        self.assertEqual([item["evidence"]["source"]["run_id"] for item in reply["tool_calls"]], ["run-a", "run-b"])

    def test_mixed_approval_is_refused_and_read_only_value_is_grounded(self):
        before = self.path.read_bytes()
        for responses in (
            (response(text="我不能批准。建议路线的预计总作业时间为 999 秒。"),),
            (response(call("get_planning_run", '{"run_id":"run-a"}')),
             response(text="已替您批准。建议路线的预计总作业时间为 105 秒。")),
        ):
            with self.subTest(responses=len(responses)):
                client = FakeClient(*responses)
                reply = answer_question(
                    "选中的方案预计总作业时间是多少？顺便帮我批准。", selected_run_id="run-a",
                    client=client, model="test-model", db_path=self.path,
                )
                self.assertIn("审批需由人工在工作台完成", reply["answer"])
                self.assertIn("建议路线总作业时间：105 秒", reply["answer"])
                self.assertNotIn("999", reply["answer"])
                self.assertNotIn("已替您批准", reply["answer"])
                self.assertEqual(len(reply["tool_calls"]), 1)
                self.assertEqual(reply["tool_calls"][0]["name"], "get_planning_run")
                self.assertEqual(len(client.requests), len(responses))
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_evidence_withholds_numbers_and_respects_remaining_tool_budget(self):
        for run_id, budget, expected_calls in ((None, 4, 0), ("run-missing", 4, 1), ("run-a", 0, 0)):
            with self.subTest(run_id=run_id, budget=budget):
                client = FakeClient(response(text="建议路线的总作业时间为 999 秒。"))
                reply = answer_question(
                    "预计总作业时间是多少？顺便帮我批准。", selected_run_id=run_id, max_tool_calls=budget,
                    client=client, model="test-model", db_path=self.path,
                )
                self.assertNotIn("999", reply["answer"])
                self.assertIn("无法确认", reply["answer"])
                self.assertIn("审批需由人工", reply["answer"])
                self.assertEqual(len(reply["tool_calls"]), expected_calls)
                self.assertEqual(len(client.requests), 1)
                if expected_calls:
                    self.assertEqual(reply["tool_calls"][0]["error_code"], "run_not_found")

    def test_grounded_analysis_keeps_the_model_explanation_and_supported_units(self):
        analysis = (
            "历史路线行驶时间为 80 秒，建议路线行驶时间为 70 秒。"
            "等待时间增加 0.25 分钟，因此总作业时间增加 5 秒。"
        )
        client = FakeClient(response(call("compare_planning_run", '{"run_id":"run-a"}')), response(text=analysis))
        reply = answer_question("分析 run-a 的路线差异。", client=client, model="test-model", db_path=self.path)
        self.assertEqual(reply["answer"], analysis)
        self.assertEqual(len(reply["tool_calls"]), 1)

    def test_identical_absolute_value_is_not_mistaken_for_the_requested_delta(self):
        client = FakeClient(response(call("compare_planning_run", '{"run_id":"run-a"}')),
                            response(text="建议路线的等待时间为 15 秒。"))
        reply = answer_question("运行 run-a 的等待时间差值是多少？", client=client, model="test-model", db_path=self.path)
        self.assertIn("等待时间差值（建议减历史）：15.0 秒", reply["answer"])
        self.assertEqual(len(reply["tool_calls"]), 1)

    def test_unsupported_unitless_number_is_not_kept_beside_a_valid_claim(self):
        client = FakeClient(response(call("get_planning_run", '{"run_id":"run-a"}')),
                            response(text="建议路线总作业时间为105秒且等待时间为999。"))
        reply = answer_question("运行 run-a 的建议路线总作业时间是多少？", client=client, model="test-model", db_path=self.path)
        self.assertIn("建议路线总作业时间：105 秒", reply["answer"])
        self.assertNotIn("999", reply["answer"])

    def test_bad_parameters_unknown_tool_and_missing_run_reach_model_as_errors(self):
        cases = (
            (call("get_planning_run", "{bad"), "invalid_arguments"),
            (call("get_planning_run", '{"run_id":42}'), "invalid_arguments"),
            (call("submit_review", '{"run_id":"run-a"}'), "unknown_tool"),
            (call("get_planning_run", '{"run_id":"missing"}'), "run_not_found"),
        )
        for request, code in cases:
            with self.subTest(code=code):
                client = FakeClient(response(request), response(text="查询无法完成。"))
                result = self.ask(client)
                self.assertEqual(result["tool_calls"][0]["error_code"], code)
                self.assertEqual(returned_tool_output(client.requests[1], "call-1")["error"]["code"], code)

    def test_tool_limit_blocks_additional_calls(self):
        first = call("get_planning_run", '{"run_id":"run-a"}')
        extra = call("get_planning_run", '{"run_id":"run-a"}', "extra")
        client = FakeClient(response(first), response(extra))
        result = self.ask(client, max_tool_calls=1)
        self.assertEqual(result["error"]["code"], "tool_limit")
        self.assertEqual([entry["call_id"] for entry in result["tool_calls"]], ["call-1"])
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(client.requests[1]["tool_choice"], "auto")
        self.assertIn("工具额度已用完", client.requests[1]["messages"][0]["content"])

    def test_default_limit_allows_four_tools_and_five_requests(self):
        requests = [response(call("get_planning_run", '{"run_id":"run-a"}', f"call-{i}")) for i in range(5)]
        client = FakeClient(*requests)
        result = self.ask(client)
        self.assertEqual(result["error"]["code"], "tool_limit")
        self.assertEqual(len(result["tool_calls"]), 4)
        self.assertEqual(len(client.requests), 5)

    def test_api_errors_preserve_prior_audit_without_exception_details(self):
        first = call("get_planning_run", '{"run_id":"run-a"}')
        for error, code in ((TimeoutError("secret fixture"), "api_timeout"), (RuntimeError("secret fixture"), "api_error")):
            with self.subTest(code=code):
                result = self.ask(FakeClient(response(first), error))
                self.assertEqual(result["error"]["code"], code)
                self.assertEqual(len(result["tool_calls"]), 1)
                self.assertNotIn("secret fixture", json.dumps(result, allow_nan=False))

    def test_distinguishes_zhipu_codes_on_the_same_http_status(self):
        cases = (
            (429, {"error": {"code": "1113"}}, None, "1113", "账户欠费"),
            (429, {"code": 1302}, None, "1302", "账户速率受限"),
            (429, None, "1305", "1305", "模型服务繁忙"),
            (429, {"code": "1308"}, None, "1308", "使用额度已达上限"),
            (429, {"code": "1311"}, None, "1311", "套餐未开放模型权限"),
            (429, {"code": "9999"}, None, "9999", "请求受限"),
            (401, {"code": "1003"}, None, "1003", "身份验证失败"),
            (403, {"error": {"code": "1220"}}, None, "1220", "访问权限不足"),
        )
        for status, body, code, expected_code, category in cases:
            with self.subTest(status=status, expected_code=expected_code):
                result = self.ask(FakeClient(api_failure(status, body, code)))
                self.assertEqual(result["error"]["code"], "api_error")
                message = result["error"]["message"]
                self.assertIn(f"HTTP {status}", message)
                self.assertIn(f"业务错误码 {expected_code}", message)
                self.assertIn(category, message)

    def test_diagnostics_never_echo_provider_text_or_invalid_code(self):
        payload = {
            "error": {"code": "1302-injected-code", "message": "body secret fixture"},
            "Authorization": "Bearer fake-secret",
            "request": {"messages": ["private request fixture"]},
        }
        result = self.ask(FakeClient(api_failure(429, payload)))
        self.assertIn("HTTP 429，业务错误码未提供", result["error"]["message"])
        text = json.dumps(result, ensure_ascii=False, allow_nan=False)
        for value in ("injected-code", "body secret fixture", "Authorization", "fake-secret", "private request fixture", "raw exception fixture"):
            self.assertNotIn(value, text)

    def test_audit_does_not_retain_configured_key(self):
        with patch.dict(os.environ, {"ZHIPU_API_KEY": "key-fixture"}):
            client = FakeClient(response(call("get_planning_run", '{"run_id":"key-fixture"}')),
                                response(text="key-fixture"))
            result = self.ask(client)
            self.assertNotIn("key-fixture", json.dumps(result, allow_nan=False))
            self.assertNotIn("key-fixture", json.dumps(client.requests[-1], allow_nan=False))

    def test_configures_sdk_from_zhipu_environment_without_openai_fallback(self):
        config = {"ZHIPU_API_KEY": "test-only", "ZHIPU_MODEL": "glm-4.7-flash", "ZHIPU_BASE_URL": "https://example.invalid/api/paas/v4"}
        client = FakeClient(response(text="已连接模拟端点。"))
        with patch.dict(os.environ, config), patch("openai.OpenAI") as sdk, patch("src.ai_assistant.load_dotenv") as dotenv:
            sdk.return_value.__enter__.return_value = client
            result = answer_question("你好", db_path=self.path)
        self.assertTrue(result["ok"])
        sdk.assert_called_once_with(api_key="test-only", base_url=config["ZHIPU_BASE_URL"], timeout=20, max_retries=0)
        self.assertEqual(client.requests[0]["model"], "glm-4.7-flash")
        dotenv.assert_not_called()

    def test_dotenv_fills_model_when_absent_and_process_environment_has_priority(self):
        dotenv_path = self.path.parent / ".env"
        dotenv_path.write_text(
            "ZHIPU_API_KEY=test-only\nZHIPU_MODEL=glm-4-flash-250414\nZHIPU_BASE_URL=https://example.invalid\n",
            encoding="utf-8",
        )
        for inherited, expected in ((None, "glm-4-flash-250414"), ("glm-4.7-flash", "glm-4.7-flash")):
            with self.subTest(inherited=inherited), patch.dict(os.environ, {}), patch("src.ai_assistant._ENV_PATH", dotenv_path):
                for field in ("ZHIPU_MODEL", "ZHIPU_BASE_URL", "ZHIPU_API_KEY"):
                    os.environ.pop(field, None)
                if inherited is not None:
                    os.environ["ZHIPU_MODEL"] = inherited
                self.assertIsNone(setup_message())
                self.assertEqual(configured_model(), expected)

    def test_missing_zhipu_key_does_not_use_openai_key_or_create_client(self):
        config = {"ZHIPU_API_KEY": "", "ZHIPU_MODEL": "glm-4.7-flash", "ZHIPU_BASE_URL": "https://example.invalid",
                  "OPENAI_API_KEY": "unrelated-test-only"}
        with patch.dict(os.environ, config), patch("src.ai_assistant.load_dotenv") as dotenv, patch("openai.OpenAI") as sdk:
            result = answer_question("你好", db_path=self.path)
        self.assertEqual(result["error"]["code"], "configuration_required")
        self.assertIn("ZHIPU_API_KEY", result["error"]["message"])
        self.assertEqual(dotenv.call_args.args[0].name, ".env")
        self.assertFalse(dotenv.call_args.kwargs["override"])
        sdk.assert_not_called()

    def test_streamlit_chat_interaction_timeout_and_safe_diagnostics(self):
        client = FakeClient(
            response(call("list_planning_runs", '{"route_id":"RouteID_test"}', "list")),
            response(call("compare_planning_run", '{"run_id":"run-a"}')),
            response(text="模拟回答：总作业时间增加 5 秒。"),
            TimeoutError("secret fixture"),
            api_failure(429, {"code": "1305", "message": "body secret fixture"}),
        )
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        ask = partial(answer_question, client=client, model="test-model")
        before = self.path.read_bytes()
        with (
            patch("src.ai_assistant.setup_message", return_value=None),
            patch("src.ai_assistant.answer_question", side_effect=lambda question, **kwargs: ask(question, **(kwargs | {"db_path": self.path}))),
            patch("src.planning_db.list_runs", side_effect=lambda _, route_id: list_runs(self.path, route_id, read_only=True)),
        ):
            page = AppTest.from_file(app_path).run()
            self.assertFalse(page.exception)
            page.chat_input[0].set_value("比较 run-a").run()
            self.assertFalse(page.exception)
            self.assertTrue(any("总作业时间增加 5 秒" in item.value for item in page.markdown))
            self.assertTrue(any("成功（返回 1 条规划记录）" in item.value for item in page.text))
            self.assertTrue(any("compare_planning_run · run_id=run-a · 成功" in item.value for item in page.text))
            page.chat_input[0].set_value("再看一下").run()
            self.assertFalse(page.exception)
            self.assertTrue(any("请求超时" in item.value for item in page.error))
            self.assertIn({"role": "user", "content": "比较 run-a"}, client.requests[-1]["messages"])
            page.chat_input[0].set_value("再次查询").run()
            self.assertFalse(page.exception)
            self.assertTrue(any("模型服务繁忙（HTTP 429，业务错误码 1305）" in item.value for item in page.error))
            self.assertFalse(any("body secret fixture" in item.value for item in page.error))
            self.assertEqual(len(page.session_state["ai_chat_turns"]), 3)
        self.assertEqual(self.path.read_bytes(), before)

    def test_streamlit_without_credentials_shows_configuration(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        with (
            patch.dict(os.environ, {"ZHIPU_API_KEY": "", "ZHIPU_MODEL": "", "ZHIPU_BASE_URL": ""}),
            patch("src.ai_assistant.load_dotenv"),
            patch("src.planning_db.list_runs", return_value=[]),
        ):
            page = AppTest.from_file(app_path).run()
        self.assertFalse(page.exception)
        self.assertTrue(page.chat_input[0].proto.disabled)
        self.assertTrue(any("ZHIPU_API_KEY" in item.value and "ZHIPU_MODEL" in item.value for item in page.info))

    def test_streamlit_switch_keeps_transcript_but_sends_only_current_context(self):
        run = get_run(self.path, "run-a", read_only=True)
        save_run(self.path, {**run, "run_id": "run-b", "suggested_metrics": {**run["suggested_metrics"], "waiting_seconds": 23}})
        client = FakeClient(response(text="等待时间为 15 秒。"), response(text="等待时间为 15 秒。"))
        with (
            patch("app.setup_message", return_value=None),
            patch("app.answer_question", side_effect=lambda question, **kwargs: answer_question(
                question, **(kwargs | {"db_path": self.path}), client=client, model="test-model",
            )),
        ):
            page = AppTest.from_string(
                "import streamlit as st\n"
                "from app import _show_ai_assistant\n"
                "_show_ai_assistant(st.selectbox('方案', ['run-a', 'run-b']))\n"
            ).run()
            page.chat_input[0].set_value("查询旧方案等待时间是多少？").run()
            page.selectbox[0].set_value("run-b").run()
            page.chat_input[0].set_value("这次的等待时间呢？").run()
        self.assertFalse(page.exception)
        turns = page.session_state["ai_chat_turns"]
        self.assertEqual([turn["selected_run_id"] for turn in turns], ["run-a", "run-b"])
        self.assertIn("23 秒", turns[1]["response"]["answer"])
        self.assertTrue(any("证据补查" in item.value for item in page.text))
        self.assertEqual(len(client.requests[1]["messages"]), 2)

    def test_streamlit_displays_and_sends_each_configured_model(self):
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        for model in ("glm-4.7-flash", "glm-4-flash-250414"):
            config = {"ZHIPU_API_KEY": "test-only", "ZHIPU_MODEL": model, "ZHIPU_BASE_URL": "https://example.invalid"}
            client = FakeClient(response(text="模拟回答"), api_failure(400, {"error": {"code": "1212"}}))
            with (
                self.subTest(model=model),
                patch.dict(os.environ, config),
                patch("openai.OpenAI") as sdk,
                patch("src.planning_db.list_runs", return_value=[]),
            ):
                sdk.return_value.__enter__.return_value = client
                page = AppTest.from_file(app_path).run()
                self.assertFalse(page.exception)
                self.assertFalse(page.chat_input[0].proto.disabled)
                self.assertTrue(any(f"智谱模型 `{model}`" in item.value for item in page.caption))
                page.chat_input[0].set_value("你好").run()
                self.assertFalse(page.exception)
                self.assertEqual(client.requests[0]["model"], model)
                self.assertTrue(any(item.value == "模拟回答" for item in page.markdown))
                page.chat_input[0].set_value("再查一次").run()
                self.assertTrue(any("当前模型不支持此调用方式（HTTP 400，业务错误码 1212）" in item.value for item in page.error))
