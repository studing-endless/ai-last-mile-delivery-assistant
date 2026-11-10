"""Turn-local evidence for planning time claims."""

import re
import unicodedata
from decimal import Decimal


_LABELS = {
    "driving_seconds": "行驶时间",
    "planned_service_time_seconds_sum": "计划服务时间",
    "waiting_seconds": "等待时间",
    "total_operation_seconds": "总作业时间",
}
_METRIC = re.compile(
    r"(?P<driving_seconds>行驶(?:时间|时长)?)|"
    r"(?P<planned_service_time_seconds_sum>(?:计划)?服务(?:时间|时长)?)|"
    r"(?P<waiting_seconds>等待(?:时间|时长)?|[多少]等)|"
    r"(?P<total_operation_seconds>(?:总作业|作业总)(?:时间|时长)?|预计作业时间)"
)
_UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_RUN = re.compile(rf"(?<![\w-])(?:{_UUID}|run-[a-z0-9_-]+)(?![\w-])", re.I | re.ASCII)
_ROUTE = re.compile(rf"RouteID_{_UUID}", re.I)
_APPROVAL = re.compile(r"(?:帮我|替我|代我|给我|顺便|直接|请)\s*(?:帮我|直接)?\s*(?:批准|审批|驳回)|(?:^|[。；，,])\s*(?:批准|审批|驳回)(?:这|该|当前|方案|运行)")
_DELTA = re.compile(r"差值|差异|变化|对比|比较|(?:比|相比).*(?:多|少|长|短|快|慢|历史|建议)|[多少]等|节省|增加|减少|缩短|延长")
_QUESTION = re.compile(r"多少|多久|差值|变化|对比|比较|[多少]等|节省|查看|查询|告诉|列出|给出|分别|[?？呢]")
_HYPOTHETICAL = re.compile(r"如果|假设|假如|反事实|延后|改成")
_UNCERTAIN = re.compile(r"并非|没有|不是|不等于|不一定|可能|无法确认|不能确认|超过|低于|不到|至少|至多")
_NUMBER = r"[+\-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|[零〇一二两三四五六七八九十百千万点]+"
_AMOUNT = re.compile(rf"(?<![\w.,-])(?P<number>{_NUMBER})\s*(?P<unit>秒钟?|分钟|小时|%|s\b|min\b|h\b)", re.ASCII)
_BARE_NUMBER = re.compile(rf"\s*(?:差值)?\s*[:=]?\s*(?:为|是|约)?\s*(?:{_NUMBER})(?![\w.-])", re.ASCII)
_CLAUSE = re.compile(r"[。；;\n，、]|(?<!\d),|,(?!\d)")
_DIRECTION = re.compile(r"增加|延长|多|长|慢|减少|缩短|节省|少|短|快")
_DECREASE = {"减少", "缩短", "节省", "少", "短", "快"}


def explicit_runs(text: str) -> set[str]:
    return set(_RUN.findall(text))


def current_run(question: str, selected: str | None, history: list | tuple) -> str | None:
    mentioned = explicit_runs(question)
    if mentioned:
        return next(iter(mentioned)) if len(mentioned) == 1 else None
    if selected:
        return selected
    for item in reversed(history[-12:]):
        if item["role"] == "user":
            previous = explicit_runs(item["content"])
            if previous:
                return next(iter(previous)) if len(previous) == 1 else None
            if item.get("run_id"):
                return item["run_id"]
    return None


def approval_requested(question: str) -> bool:
    return bool(_APPROVAL.search(question))


def requested_metrics(question: str) -> dict[str, tuple[str, ...]]:
    if _HYPOTHETICAL.search(question):
        return {}
    if _DELTA.search(question):
        views = ("delta",)
    elif "历史" in question:
        views = ("historical", "suggested") if "建议" in question else ("historical",)
    else:
        views = ("suggested",)
    asks_for_values = _QUESTION.search(question)
    return {
        match.lastgroup: views for match in _METRIC.finditer(question)
        if asks_for_values or match.end() == len(question)
    }


def snapshot_evidence(result: dict) -> dict | None:
    """Keep only the time fields supplied by a successful tool for one saved run."""
    source = result.get("source") or {}
    data = result["data"] if result["ok"] else None
    if not data or not source.get("run_id") or source["run_id"] != data.get("run_id"):
        return None
    metrics = {}
    for field in _LABELS:
        if "metrics" in data:
            values = data["metrics"].get(field, {})
            if values.get("unit") == "seconds":
                metrics[field] = values
        elif data["units"].get(field) == "seconds":
            historical = (data["historical_metrics"] or {}).get(field)
            suggested = (data["suggested_metrics"] or {}).get(field)
            delta = None
            if historical is not None and suggested is not None:
                delta = float(Decimal(str(suggested)) - Decimal(str(historical)))
            metrics[field] = {"historical": historical, "suggested": suggested, "delta": delta, "unit": "seconds"}
    return {"source": source, "metrics": metrics}


def latest_listed_run(question: str, audit: list[dict]) -> str | None:
    routes = set(_ROUTE.findall(question))
    if len(routes) != 1 or not re.search(r"最新|最近一次", question):
        return None
    matches = {entry.get("latest_run_id") for entry in audit if entry.get("result_route_id") in routes and "latest_run_id" in entry}
    return next(iter(matches)) if len(matches) == 1 else None


def _matches_quantity(match, expected, direction: str | None, *, reverse: bool) -> bool:
    unit = match["unit"]
    raw = match["number"].replace(",", "")
    if unit == "%" or not re.search(r"\d", raw) or expected is None:
        return False
    actual = Decimal(raw)
    if direction:
        decreasing = direction in _DECREASE
        if not actual or (raw.startswith(("+", "-")) and (actual < 0) != decreasing):
            return False
        actual = (-1 if decreasing else 1) * abs(actual)
    if reverse:
        actual = -actual
    factor = {"分钟": 60, "min": 60, "小时": 3600, "h": 3600}.get(unit, 1)
    tolerance = {1: Decimal("0.05"), 60: Decimal("0.30"), 3600: Decimal("1.8")}[factor]
    return abs(actual * factor - Decimal(str(expected))) <= tolerance


def check_time_claims(answer: str, metrics: dict, requested: dict, run_id: str | None) -> tuple[bool, dict]:
    """Recognize bounded time claims, using the question only to disambiguate an omitted side."""
    answer = unicodedata.normalize("NFKC", answer).replace("−", "-")
    answer = answer.translate(str.maketrans("", "", "*\x60"))
    other_runs = explicit_runs(answer) - {run_id}
    found = {}
    valid = True
    for clause in _CLAUSE.split(answer):
        amounts = list(_AMOUNT.finditer(clause))
        unit_bearing_ends = {amount.end("number") for amount in amounts}
        for label in _METRIC.finditer(clause):
            bare = _BARE_NUMBER.match(clause, label.end())
            if bare and bare.end() not in unit_bearing_ends:
                valid = False
        if not amounts:
            continue
        if not run_id or other_runs or _HYPOTHETICAL.search(clause) or _UNCERTAIN.search(clause):
            valid = False
        previous_amount = 0
        for amount in amounts:
            local = clause[previous_amount:amount.start()]
            local_labels = list(_METRIC.finditer(local))
            previous_amount = amount.end()
            if len({label.lastgroup for label in local_labels}) != 1:
                valid = False
                continue
            label = local_labels[-1]
            field = label.lastgroup
            direction_match = _DIRECTION.search(local[label.end():])
            direction = direction_match[0] if direction_match else None
            if label[0] in {"多等", "少等"}:
                direction = label[0][0]
            sides = re.findall(r"历史|建议", local)
            reverse = "比" in local and sides[:2] == ["历史", "建议"]
            if direction or "差值" in local or ("比" in local and len(sides) == 2):
                view = "delta"
            elif sides:
                view = "historical" if sides[-1] == "历史" else "suggested"
            else:
                views = requested.get(field, ("suggested",))
                if len(views) != 1:
                    valid = False
                    continue
                view = "historical" if views == ("historical",) else "suggested"
            if not _matches_quantity(amount, metrics.get(field, {}).get(view), direction, reverse=reverse):
                valid = False
            views = found.setdefault(field, [])
            if view not in views:
                views.append(view)
    return valid, found


def format_evidence(run_id: str | None, fields: dict, metrics: dict) -> str:
    if run_id is None:
        return "无法确认所问规划指标。请提供明确的 run_id。"
    lines = []
    for field, views in fields.items():
        for view in views:
            value = metrics.get(field, {}).get(view)
            label = _LABELS[field]
            if view == "delta":
                label += "差值（建议减历史）"
            else:
                label = ("历史路线" if view == "historical" else "建议路线") + label
            lines.append(f"- {label}：{'无法确认' if value is None else f'{value} 秒'}")
    if not lines:
        return "无法确认回答中的规划指标。请明确需要查询的指标。"
    return f"运行 {run_id} 的离线模拟估计：\n" + "\n".join(lines)
