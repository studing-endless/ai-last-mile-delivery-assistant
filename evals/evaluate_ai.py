"""Evaluate the existing read-only assistant with a fixed case set."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from types import SimpleNamespace

from src.ai_assistant import MAX_TOOL_CALLS, REQUEST_TIMEOUT_SECONDS, answer_question, configured_model, redact_api_key, setup_message
from src.ai_tools import DEFAULT_DB_PATH, TOOL_DEFINITIONS, dispatch_tool


CASE_FILE = Path(__file__).with_name("ai_cases.json")
REPORT_DIR = DEFAULT_DB_PATH.parent / "ai_evals"
EVAL_VERSION = "1.0"
MAX_LIVE_CASES = 3
MAX_LIVE_REQUESTS = 20
TOOL_FIELDS = {item["name"]: item["parameters"]["required"][0] for item in TOOL_DEFINITIONS}
SERVICE_ERRORS = {"api_error", "api_timeout", "client_error", "configuration_required"}
UNITS = {"seconds": "秒", "records": "条", "packages": "个", "stops": "个"}
ID_PATTERN = re.compile(r"(?:RouteID_)?[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", re.I)
NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def safe_text(value: str) -> str:
    text = redact_api_key(value)
    if base_url := os.environ.get("ZHIPU_BASE_URL", "").strip():
        text = text.replace(base_url, "[接口地址已隐藏]")
    text = re.sub(r"(?im)\b(?:authorization|(?:zhipu_|openai_)?api[_ -]?key)\b[^\r\n]*", "[凭据字段已隐藏]", text)
    return re.sub(r"(?i)\b(?:bearer\s+\S+|sk-[\w-]{8,})", "[凭据已隐藏]", text)


def safe_id(value: str) -> str:
    text = safe_text(value)
    return text if ID_PATTERN.fullmatch(text) else "[非标准业务标识已隐藏]"


def safe_audit(audit: list[dict]) -> list[dict]:
    trace = []
    for index, call in enumerate(audit, start=1):
        name = safe_text(call["name"])
        query = call["query"]
        entry = {
            "index": index,
            "name": name if re.fullmatch(r"[a-z_][a-z0-9_]{0,63}", name) else "[工具名称已隐藏]",
            "arguments": {field: safe_id(value) for field, value in query.items()} if query else None,
            "ok": call["ok"],
            "error_code": call["error_code"],
        }
        if "result_count" in call:
            entry["result_count"] = call["result_count"]
        if "result_route_id" in call:
            entry["result_route_id"] = safe_id(call["result_route_id"])
        trace.append(entry)
    return trace


@contextmanager
def frozen_database(path: Path):
    """Copy through a read-only SQLite connection so every case sees the same state."""
    with TemporaryDirectory(prefix="last-mile-eval-") as directory:
        snapshot = Path(directory) / "snapshot.sqlite3"
        uri = path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as source, closing(sqlite3.connect(snapshot)) as target:
            source.backup(target)
        yield snapshot


def reference_value(result: dict, path: str):
    value = result
    for part in path.split("."):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def prepare_references(cases: list[dict], db_path: Path) -> dict:
    references = {}
    cached = {}
    for case in cases:
        for index, turn in enumerate(case["turns"], start=1):
            reference = turn.get("reference")
            result = None
            for option in turn["tool_options"]:
                name, query = option["name"], option["query"]
                if name not in TOOL_FIELDS or set(query) != {TOOL_FIELDS[name]}:
                    raise ValueError(f"Invalid tool expectation in {case['case_id']} turn {index}")
                key = (name, canonical_json(query))
                if key not in cached:
                    cached[key] = dispatch_tool(name, query, db_path=db_path)
                output = cached[key]
                outcome = "ok" if output["ok"] else output["error"]["code"]
                if outcome != option["outcome"]:
                    raise ValueError(f"SQLite reference unavailable or changed in {case['case_id']} turn {index} ({outcome})")
                if option == reference:
                    result = output
            if bool(turn["tool_options"]) != (result is not None):
                raise ValueError(f"Missing reference in {case['case_id']} turn {index}")
            numbers = {}
            for label, spec in turn.get("numbers", {}).items():
                expected = reference_value(result, spec["path"])
                if spec["unit"] == "records" and isinstance(expected, list):
                    expected = len(expected)
                if spec["unit"] not in UNITS or type(expected) not in (float, int):
                    raise ValueError(f"Invalid numeric reference in {case['case_id']} turn {index}")
                numbers[label] = {**spec, "expected": expected}
            evidence = {label: reference_value(result, path) for label, path in turn.get("evidence", {}).items()}
            if any(evidence[label] != value for label, value in turn.get("required_evidence", {}).items()):
                raise ValueError(f"Fixed SQLite evidence changed in {case['case_id']} turn {index}")
            references.setdefault(case["case_id"], []).append({
                "source": reference, "numbers": numbers, "review_values": evidence,
            })
    return references


def check_tools(options: list[dict], audit: list[dict]) -> dict:
    if not options:
        return {
            "necessary_tool_selected": not audit,
            "business_arguments_correct": None,
            "expected_tool_outcome": None,
            "wrong_argument_call_indices": [],
            "redundant_call_indices": list(range(1, len(audit) + 1)),
        }
    candidates = [(index, call, option) for index, call in enumerate(audit, start=1)
                  for option in options if call["name"] == option["name"]]
    matched = [(index, call, option) for index, call, option in candidates if call["query"] == option["query"]]
    resolved = next((index for index, call, option in matched
                     if ("ok" if call["ok"] else call["error_code"]) == option["outcome"]), None)
    wrong_arguments = {index for index, _, _ in candidates} - {index for index, _, _ in matched}
    return {
        "necessary_tool_selected": bool(candidates),
        "business_arguments_correct": bool(matched),
        "expected_tool_outcome": resolved is not None,
        "wrong_argument_call_indices": sorted(wrong_arguments),
        "redundant_call_indices": [index for index in range(1, len(audit) + 1) if index != resolved] if resolved else [],
    }


def check_number(answer: str, label: str, spec: dict) -> dict:
    """Check explicitly associated, unit-bearing claims. Leave unmatched prose for review."""
    unit = spec["unit"]
    units = r"分钟|秒" if unit == "seconds" else re.escape(UNITS[unit])
    text = unicodedata.normalize("NFKC", answer).replace("−", "-")
    text = text.translate(str.maketrans("", "", "*\x60"))
    # The separators support simple prose, bullets and labelled Markdown table cells.
    separator = r"[\s|:=]*(?:为|是)?\s*"
    heading = re.escape(label) + separator
    if label.endswith("差值"):
        # For a shortened delta label require a direction, so a route's absolute time is not mistaken for a delta.
        heading = f"(?:{heading}|{re.escape(label.removesuffix('差值'))}{separator}(?=增加|减少|缩短|延长))"
    pattern = heading + r"(?P<direction>增加|减少|缩短|延长)?(?:了)?\s*(?:大约|约为|约)?\s*" + (
        r"(?P<value>" + NUMBER + r")\s*(?P<unit>" + units + r")"
    )
    expected = Decimal(str(spec["expected"]))
    claims = []
    for match in re.finditer(pattern, text):
        raw = match["value"].replace(",", "")
        value = Decimal(raw)
        direction = match["direction"]
        negative = direction in {"减少", "缩短"}
        consistent_sign = not direction or not value or not raw.startswith(("+", "-")) or (value < 0) == negative
        if negative:
            value = -abs(value)
        factor = 60 if match["unit"] == "分钟" else 1
        tolerance = Decimal("0.30") if factor == 60 else Decimal("0.05") if unit == "seconds" else Decimal(0)
        claims.append({
            "value": str(value), "unit": match["unit"], "tolerance_in_reference_unit": str(tolerance),
            "matches": consistent_sign and abs(value * factor - expected) <= tolerance,
        })
    status = "unverified" if not claims else "match" if all(item["matches"] for item in claims) else "mismatch"
    return {"label": label, **spec, "claims": claims, "status": status}


class CountedClient:
    """Count completions attempts, including failures, and enforce the total budget."""

    def __init__(self, create, limit: int):
        self._create = create
        self.limit = limit
        self.requests = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **request):
        if self.requests >= self.limit:
            raise RuntimeError("Evaluation request budget exhausted")
        self.requests += 1
        return self._create(**request)


def mock_client(turn: dict, reference: dict) -> CountedClient:
    """Build scripted Chat Completions fixtures. These provide no model-quality evidence."""
    replies = []
    if options := turn["tool_options"]:
        call = options[0]
        replies.append(SimpleNamespace(content=None, tool_calls=[
            SimpleNamespace(
                id="mock-call-1", type="function",
                function=SimpleNamespace(name=call["name"], arguments=canonical_json(call["query"])),
            ),
        ]))
    parts = [turn["mock_answer"].format_map(reference["review_values"])]
    parts.extend(f"{label}：{spec['expected']} {UNITS[spec['unit']]}。" for label, spec in reference["numbers"].items())
    replies.append(SimpleNamespace(content="".join(parts), tool_calls=[]))
    messages = iter(replies)

    def create(**_request):
        message = next(messages)
        choice = SimpleNamespace(message=message, finish_reason="tool_calls" if message.tool_calls else "stop")
        return SimpleNamespace(choices=[choice])

    return CountedClient(create, MAX_TOOL_CALLS + 1)


def grade_answer(turn: dict, reference: dict, audit: list[dict], answer: str, *, assessable: bool) -> tuple[dict, list[dict]]:
    tools = check_tools(turn["tool_options"], audit)
    numbers = [check_number(answer, label, spec) for label, spec in reference["numbers"].items()] if assessable else []
    return tools, numbers


def assessment_status(tools: dict, numbers: list[dict], *, code=None, backend_failure=False, permission_attempt=False) -> str:
    if code in SERVICE_ERRORS:
        return "service_failure"
    if code:
        return "response_failure"
    if backend_failure:
        return "tool_backend_failure"
    tool_failure = any(tools[key] is False for key in ("necessary_tool_selected", "business_arguments_correct", "expected_tool_outcome"))
    if tool_failure or tools["wrong_argument_call_indices"] or permission_attempt or any(item["status"] == "mismatch" for item in numbers):
        return "automatic_failure"
    return "needs_review"


def evaluate_turn(turn: dict, reference: dict, history: list[dict], client: CountedClient, model: str, db_path: Path, *, grader=grade_answer) -> tuple[dict, dict]:
    start = perf_counter()
    before = client.requests
    reply = answer_question(
        turn["question"], selected_run_id=turn.get("selected_run_id"), history=history,
        db_path=db_path, client=client, model=model,
    )
    error = reply["error"]
    code = error["code"] if error else None
    audit = reply["tool_calls"]
    answer = safe_text(reply["answer"])
    tools, numeric = grader(turn, reference, audit, answer, assessable=reply["ok"])
    permission_attempt = any(call["name"] not in TOOL_FIELDS for call in audit)
    status = assessment_status(
        tools, numeric, code=code, backend_failure=any(call["error_code"] == "query_failed" for call in audit),
        permission_attempt=permission_attempt,
    )
    details = error["message"] if error else ""
    http = re.search(r"\bHTTP ([1-5]\d{2})\b", details)
    provider = re.search(r"业务错误码 (\d{4})\b", details)
    entry = {
        "question": safe_text(turn["question"]),
        "selected_run_id": safe_id(turn["selected_run_id"]) if turn.get("selected_run_id") else None,
        "status": status,
        "tool_calls": safe_audit(audit),
        "tool_checks": {**tools, "assessable": reply["ok"]},
        "answer": answer,
        "number_checks": numeric,
        "read_only": {"attempted_unlisted_tool": permission_attempt, "write_tools_exposed": False},
        "api_error": {"code": code, "http_status": int(http[1]) if http else None,
                      "provider_code": provider[1] if provider else None} if code else None,
        "api_requests": client.requests - before,
        "elapsed_seconds": round(perf_counter() - start, 6),
        "reference": reference,
        "manual_review": {
            "criteria": turn["review"], "answer_supported": None, "unsupported_business_claims": None,
            "claims_unavailable_action_executed": None, "notes": None,
            "status": "pending" if reply["ok"] else "not_applicable",
        },
    }
    return entry, reply


def run_suite(cases: list[dict], references: dict, db_path: Path, *, model: str, live_client: CountedClient | None = None, grader=grade_answer) -> list[dict]:
    results = []
    service_failed = False
    for case in cases:
        case_id = case["case_id"]
        turns = []
        history = []
        previous_failed = False
        for turn, reference in zip(case["turns"], references[case_id], strict=True):
            if service_failed or previous_failed:
                status = "not_run_after_service_failure" if service_failed else "not_run_after_turn_failure"
                turns.append({"question": safe_text(turn["question"]), "status": status, "api_requests": 0})
                continue
            client = live_client if live_client is not None else mock_client(turn, reference)
            entry, reply = evaluate_turn(turn, reference, history, client, model, db_path, grader=grader)
            turns.append(entry)
            service_failed = live_client is not None and entry["status"] == "service_failure"
            previous_failed = not reply["ok"]
            if reply["ok"]:
                history.extend([{"role": "user", "content": turn["question"]}, {"role": "assistant", "content": reply["answer"]}])
        results.append({
            "case_id": case_id, "category": case["category"], "turns": turns,
            "api_requests": sum(turn["api_requests"] for turn in turns),
            "elapsed_seconds": round(sum(turn.get("elapsed_seconds", 0) for turn in turns), 6),
        })
    return results


def write_report(report: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    version = "" if report["eval_version"] == EVAL_VERSION else f"-v{report['eval_version']}"
    path = REPORT_DIR / f"{report['mode']}{version}-{stamp}.json"
    with path.open("x", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, allow_nan=False, indent=2)
        file.write("\n")
    return path


def main(argv: list[str] | None = None, *, eval_version: str = EVAL_VERSION, grader=grade_answer, replay=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly enable real, sequential Zhipu calls.")
    parser.add_argument("--case", action="append", metavar="C01", help="Select an explicit case ID. Repeat to select more.")
    parser.add_argument("--max-requests", type=int, default=MAX_LIVE_REQUESTS, help="Upper bound for live completions attempts.")
    if replay is not None:
        parser.add_argument("--replay", type=Path, help="Score a saved v1 report without accessing a model or database.")
    args = parser.parse_args(argv)
    if replay is not None and args.replay:
        if args.live or args.case or args.max_requests != MAX_LIVE_REQUESTS:
            parser.error("Replay cannot be combined with live or case selection options.")
        try:
            report = replay(args.replay)
            path = write_report(report)
        except (OSError, ValueError, KeyError, TypeError) as error:
            parser.exit(2, f"Replay failed ({type(error).__name__}).\n")
        print(f"Replay: {len(report['case_ids'])} cases, 0 external requests. Report: {path}")
        return 0
    raw = CASE_FILE.read_bytes()
    suite = json.loads(raw)
    all_cases = {case["case_id"]: case for case in suite["cases"]}
    selected = args.case or list(all_cases)
    if len(all_cases) != len(suite["cases"]) or len(set(selected)) != len(selected) or any(case not in all_cases for case in selected):
        parser.error("Case IDs must be known and unique.")
    cases = [all_cases[case_id] for case_id in selected]
    worst_case = sum(len(case["turns"]) for case in cases) * (MAX_TOOL_CALLS + 1)
    if args.live and (not args.case or len(cases) > MAX_LIVE_CASES or not worst_case <= args.max_requests <= MAX_LIVE_REQUESTS):
        parser.error("Live mode requires 1–3 explicit cases and enough budget for up to 5 requests per turn (at most 20 total).")

    started_at = datetime.now(UTC).isoformat()
    try:
        with frozen_database(DEFAULT_DB_PATH) as snapshot:
            references = prepare_references(cases, snapshot)
            if args.live:
                if setup_message():
                    parser.exit(2, "Live configuration unavailable. Check the Streamlit assistant configuration.\n")
                model = configured_model()
                from openai import OpenAI

                try:
                    sdk = OpenAI(
                        api_key=os.environ["ZHIPU_API_KEY"].strip(), base_url=os.environ["ZHIPU_BASE_URL"].strip(),
                        timeout=REQUEST_TIMEOUT_SECONDS, max_retries=0,
                    )
                except Exception:
                    parser.exit(2, "Live client could not be initialized. No API request was made.\n")
                with sdk as active_sdk:
                    client = CountedClient(active_sdk.chat.completions.create, args.max_requests)
                    results = run_suite(cases, references, snapshot, model=model, live_client=client, grader=grader)
            else:
                model = "offline-scripted-fixture"
                results = run_suite(cases, references, snapshot, model=model, grader=grader)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f"Evaluation setup or local data failed ({type(error).__name__}).\n")

    # Redact evidence strings separately, without retaining raw tool responses.
    for entries in references.values():
        for reference in entries:
            reference["review_values"] = {
                label: safe_text(value) if isinstance(value, str) else value
                for label, value in reference["review_values"].items()
            }
    turns = [turn for result in results for turn in result["turns"]]
    statuses = {status: sum(turn["status"] == status for turn in turns) for status in sorted({turn["status"] for turn in turns})}
    offline_passed = None if args.live else sum(
        len(result["turns"]) == len(case["turns"]) and all(
            turn["status"] == "needs_review" and not turn["tool_checks"]["redundant_call_indices"]
            and all(item["status"] == "match" for item in turn["number_checks"])
            for turn in result["turns"]
        )
        for case, result in zip(cases, results, strict=True)
    )
    report = {
        "mode": "live" if args.live else "offline_mock",
        "model": model, "eval_version": eval_version, "case_set_version": suite["version"],
        "case_set_sha256": hashlib.sha256(raw).hexdigest(),
        "reference_sha256": hashlib.sha256(canonical_json(references).encode("utf-8")).hexdigest(),
        "started_at_utc": started_at, "case_ids": selected, "results": results,
        "request_budget": args.max_requests if args.live else worst_case,
        "api_requests": sum(turn["api_requests"] for turn in turns),
        "external_requests": sum(turn["api_requests"] for turn in turns) if args.live else 0,
        "turn_statuses": statuses, "offline_cases_passed": offline_passed,
    }
    path = write_report(report)
    print(f"{report['mode']}: {len(cases)} cases, {report['api_requests']} completions attempts, "
          f"{report['external_requests']} external requests, offline cases passed: {offline_passed}")
    print(f"Report: {path}")
    return int(offline_passed != len(cases)) if not args.live else 2 * int("service_failure" in statuses)


if __name__ == "__main__":
    raise SystemExit(main())
