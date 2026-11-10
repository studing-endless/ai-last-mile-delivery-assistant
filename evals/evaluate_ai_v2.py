"""Version 2 scoring for the frozen AI cases and saved v1 traces."""

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from . import evaluate_ai as v1


EVAL_VERSION = "2.0"
DIRECTION = {"增加": 1, "延长": 1, "减少": -1, "缩短": -1}
COMPARISON = {"长": 1, "短": -1, "多": 1, "少": -1, **DIRECTION}
ROUTE = r"(?:历史|建议)路线"
UNCERTAIN = re.compile(r"如果|假如|假设|假定|若|可能|也许|未必|不一定|或者|并非|不是|并未|没有|无法确定")
TIME_VERBS = {"等待时间": r"等待|等", "行驶时间": "行驶"}
HIDDEN_ID = "[非标准业务标识已隐藏]"


def check_tools(options: list[dict], audit: list[dict]) -> dict:
    if not options:
        return v1.check_tools(options, audit)

    targets = [option["query"] for option in options]
    target_fields = {field for query in targets for field in query}
    correct, wrong, hidden = [], [], []
    accepted = False
    matched_outcomes = []
    for index, call in enumerate(audit, start=1):
        name, query = call["name"], call["query"]
        if v1.TOOL_FIELDS.get(name) in target_fields:
            if query and HIDDEN_ID in query.values():
                hidden.append(index)
            elif query in targets:
                correct.append(index)
            else:
                wrong.append(index)
        for option in options:
            if name != option["name"]:
                continue
            accepted = True
            if query == option["query"]:
                outcome = "ok" if call["ok"] else call["error_code"]
                matched_outcomes.append((index, outcome == option["outcome"]))

    resolved = next((index for index, ok in matched_outcomes if ok), None)
    arguments_correct = False if wrong else True if correct and not hidden else None
    return {
        "necessary_tool_selected": accepted,
        "business_arguments_correct": arguments_correct,
        "expected_tool_outcome": resolved is not None if matched_outcomes else None,
        "correct_argument_call_indices": correct,
        "wrong_argument_call_indices": wrong,
        "unverified_argument_call_indices": hidden,
        "redundant_call_indices": [i for i in range(1, len(audit) + 1) if i != resolved] if resolved else [],
    }


def clauses(answer: str):
    text = unicodedata.normalize("NFKC", answer).replace("−", "-")
    text = text.translate(str.maketrans("", "", "*\x60"))
    parts = re.split(r"([。!?;\n])", text)
    for sentence, ending in zip(parts[::2], [*parts[1::2], ""], strict=True):
        if ending == "?" or UNCERTAIN.search(sentence):
            continue
        # Leave a comma between digits intact for thousands-separated values.
        for clause in re.split(r"(?<!\d),|,(?!\d)", sentence):
            clause = re.sub(r"^\s*(?:[-•]\s+|\d+[.)]\s+)", "", clause)
            yield clause.strip(" .|\r\t")


def number_claim(match, expected: Decimal, unit: str, rule: str, *, direction=None, orientation=1) -> dict:
    raw = match["value"].replace(",", "")
    value = Decimal(raw)
    consistent = not direction or not value or not raw.startswith(("+", "-")) or (value < 0) == (direction < 0)
    if direction:
        value = direction * abs(value)
    value *= orientation
    if rule == "route_comparison" and not value:
        consistent = False  # A strict longer/shorter claim cannot have zero difference.
    factor = 60 if match["unit"] == "分钟" else 1
    tolerance = Decimal("0.30") if factor == 60 else Decimal("0.05") if unit == "seconds" else Decimal(0)
    return {
        "value": str(value), "unit": match["unit"], "rule": rule,
        "tolerance_in_reference_unit": str(tolerance),
        "matches": consistent and abs(value * factor - expected) <= tolerance,
    }


def check_number(answer: str, label: str, spec: dict) -> dict:
    unit = spec["unit"]
    units = r"分钟|秒" if unit == "seconds" else re.escape(v1.UNITS[unit])
    amount = r"(?:了)?\s*(?:大约|约为|约)?\s*(?P<value>" + v1.NUMBER + r")\s*(?P<unit>" + units + r")"
    separator = r"[\s|:=]*(?:为|是)?\s*"
    direction = "|".join(DIRECTION)
    heading = re.escape(label) + separator
    patterns = []
    if unit == "seconds" and label.endswith("差值"):
        metric = label.removesuffix("差值")
        escaped = re.escape(metric)
        heading = rf"(?:建议路线(?:的)?\s*(?:预计)?\s*)?(?:{heading}|{escaped}{separator}(?={direction}))"
        subject = rf"(?P<subject>{ROUTE})"
        baseline = rf"比\s*(?P<baseline>{ROUTE})"
        metric_label = rf"(?:的)?\s*(?:预计)?\s*{escaped}"
        comparison = "|".join(COMPARISON)
        relation = rf"\s*(?:更)?(?P<relation>{comparison})"
        patterns = [
            rf"{subject}{metric_label}\s*{baseline}(?:{metric_label})?{relation}{amount}",
            rf"{subject}\s*{baseline}{metric_label}{relation}{amount}",
        ]
        if verb := TIME_VERBS.get(metric):
            patterns.append(rf"{subject}\s*{baseline}\s*(?P<relation>多|少)\s*(?:{verb}){amount}")
    direct = heading + rf"(?P<direction>{direction})?" + amount

    expected = Decimal(str(spec["expected"]))
    claims = []
    for clause in clauses(answer):
        for pattern in patterns:
            match = re.fullmatch(pattern, clause)
            if match and match["subject"] != match["baseline"]:
                claims.append(number_claim(
                    match, expected, unit, "route_comparison", direction=COMPARISON[match["relation"]],
                    orientation=1 if match["subject"] == "建议路线" else -1,
                ))
                break
        else:
            if match := re.fullmatch(direct, clause):
                claims.append(number_claim(match, expected, unit, "direct", direction=DIRECTION.get(match["direction"])))

    status = "unverified" if not claims else "match" if all(claim["matches"] for claim in claims) else "mismatch"
    return {"label": label, **spec, "claims": claims, "status": status}


def grade_answer(turn: dict, reference: dict, audit: list[dict], answer: str, *, assessable: bool) -> tuple[dict, list[dict]]:
    tools = check_tools(turn["tool_options"], audit)
    numbers = [check_number(answer, label, spec) for label, spec in reference["numbers"].items()] if assessable else []
    return tools, numbers


def replay_report(path: Path) -> dict:
    """Regrade a saved trace using its archived evidence and the unchanged case set."""
    raw = path.read_bytes()
    source = json.loads(raw)
    case_bytes = v1.CASE_FILE.read_bytes()
    suite = json.loads(case_bytes)
    if (
        source["eval_version"] != v1.EVAL_VERSION or source["mode"] not in {"live", "offline_mock"}
        or source["case_set_version"] != suite["version"]
        or source["case_set_sha256"] != hashlib.sha256(case_bytes).hexdigest()
    ):
        raise ValueError("Replay requires a v1 report with the matching frozen case set")
    cases = {case["case_id"]: case for case in suite["cases"]}
    if source["case_ids"] != [entry["case_id"] for entry in source["results"]]:
        raise ValueError("Replay case order does not match the source trace")

    results = []
    references = {}
    complete = True
    for entry in source["results"]:
        case_id = entry["case_id"]
        turns = []
        references[case_id] = []
        for turn, recorded in zip(cases[case_id]["turns"], entry["turns"], strict=True):
            if v1.safe_text(turn["question"]) != recorded["question"]:
                raise ValueError("Recorded question does not match the frozen case")
            if "tool_checks" not in recorded:
                turns.append({"question": recorded["question"], "status": recorded["status"]})
                complete = False
                continue
            audit = [{**call, "query": call["arguments"]} for call in recorded["tool_calls"]]
            reference = recorded["reference"]
            references[case_id].append(reference)
            assessable = recorded["tool_checks"]["assessable"]
            answer = v1.safe_text(recorded["answer"])
            tools, numbers = grade_answer(turn, reference, audit, answer, assessable=assessable)
            status = v1.assessment_status(
                tools, numbers, code=(recorded["api_error"] or {}).get("code"),
                backend_failure=any(call["error_code"] == "query_failed" for call in audit),
                permission_attempt=recorded["read_only"]["attempted_unlisted_tool"],
            )
            turns.append({
                "question": recorded["question"], "status": status, "api_error": recorded["api_error"],
                "tool_calls": v1.safe_audit(audit), "answer": answer, "reference": reference,
                "tool_checks": {**tools, "assessable": assessable}, "number_checks": numbers,
                "manual_review": recorded["manual_review"],
                "original_assessment": {key: recorded[key] for key in ("status", "tool_checks", "number_checks")},
            })
        results.append({"case_id": case_id, "turns": turns})
    if complete and hashlib.sha256(v1.canonical_json(references).encode("utf-8")).hexdigest() != source["reference_sha256"]:
        raise ValueError("Recorded evidence hash does not match the source trace")
    return {
        "mode": "replay", "eval_version": EVAL_VERSION, "case_set_version": suite["version"],
        "case_set_sha256": source["case_set_sha256"], "reference_sha256": source["reference_sha256"],
        "replayed_at_utc": datetime.now(UTC).isoformat(), "model": source["model"],
        "source_report": {
            "filename": path.name, "sha256": hashlib.sha256(raw).hexdigest(),
            "eval_version": source["eval_version"], "mode": source["mode"],
            "api_requests": source["api_requests"], "external_requests": source["external_requests"],
        },
        "case_ids": source["case_ids"], "results": results, "api_requests": 0, "external_requests": 0,
    }


def main(argv: list[str] | None = None) -> int:
    return v1.main(argv, eval_version=EVAL_VERSION, grader=grade_answer, replay=replay_report)


if __name__ == "__main__":
    raise SystemExit(main())
