"""Read-only tool calls over saved planning simulations."""

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from math import isfinite
from pathlib import Path
from sys import float_info

from .planning_db import get_review, get_run, list_runs


DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "outputs" / "planning_runs.sqlite3"
_TIME_METRICS = (
    "driving_seconds", "planned_service_time_seconds_sum", "waiting_seconds", "total_operation_seconds",
)
_RISK_METRICS = ("risk_package_count", "risk_stop_count", "window_conflict_stop_count")
_COMPARE_METRICS = (*_TIME_METRICS, *_RISK_METRICS)
_METRIC_UNITS = {
    **dict.fromkeys(_TIME_METRICS, "seconds"),
    "risk_package_count": "packages",
    "risk_stop_count": "stops",
    "window_conflict_stop_count": "stops",
}


def _json_value(value):
    if isinstance(value, float) and not isfinite(value):
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def _response(
    data: dict | None = None, *, source: dict | None = None, code: str | None = None, message: str = "",
) -> dict:
    return {
        "ok": code is None,
        "scope": "offline_simulation",
        "notice": "效果均为离线模拟估计。",
        "source": source,
        "data": _json_value(data),
        "error": {"code": code, "message": message} if code else None,
    }


def _valid_id(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 256


def _invalid_id(field: str) -> dict:
    return _response(code="invalid_arguments", message=f"{field} 必须是长度不超过 256 的非空字符串。")


def _query_error(source: dict) -> dict:
    return _response(
        source=source, code="query_failed", message="无法读取规划数据库或保存的快照。请检查数据库文件和表结构。",
    )


def _started_at(value: str | None) -> datetime | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if "T" not in value and " " not in value:
        return None  # A date without a time does not identify a run's start.
    try:
        started_at = datetime.fromisoformat(value)
        if started_at.tzinfo is None:
            return started_at.replace(tzinfo=UTC)  # The stored field is started_at_utc.
        return started_at.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def list_planning_runs(route_id: str, *, db_path: Path = DEFAULT_DB_PATH) -> dict:
    """List saved runs for an Amazon Route ID. An unmatched route has an empty list."""
    if not _valid_id(route_id):
        return _invalid_id("route_id")
    route_id = route_id.strip()
    source = {
        "type": "sqlite_planning_snapshot",
        "tables": ["planning_runs"],
        "route_id": route_id,
        "run_id_field": "data.runs[].run_id",
    }
    try:
        runs = list_runs(db_path, route_id, read_only=True)
    except (sqlite3.Error, OSError):
        return _query_error(source)
    timed_runs = [(_started_at(run.get("started_at_utc")), run) for run in runs]
    timed_runs.sort(key=lambda item: (item[0] is not None, item[0], item[1]["run_id"]), reverse=True)
    latest_run_id = timed_runs[0][1]["run_id"] if timed_runs and timed_runs[0][0] is not None else None
    return _response({
        "route_id": route_id,
        "runs": [run for _, run in timed_runs],
        "record_count": len(timed_runs),
        "latest_run_id": latest_run_id,
        "undated_record_count": sum(start is None for start, _ in timed_runs),
        "sort_order": [
            {
                "field": "started_at_utc", "direction": "descending", "compare_as": "utc_datetime",
                "timezone_if_unspecified": "UTC", "missing_or_invalid": "last",
            },
            {"field": "run_id", "direction": "descending"},
        ],
        "units": {"solve_seconds": "seconds", "search_budget_seconds": "seconds"},
    }, source=source)


def _metric_value(field: str, value):
    if field in _RISK_METRICS:
        return value if type(value) is int and value >= 0 else None
    if type(value) in (int, float) and 0 <= value <= float_info.max:
        return value
    return None


def _metrics(snapshot: dict | None) -> dict | None:
    if snapshot is None:
        return None
    if not isinstance(snapshot, dict):
        raise ValueError("Invalid metric snapshot")
    return {
        **{field: _metric_value(field, snapshot.get(field)) for field in _METRIC_UNITS},
        "return_datetime_utc": snapshot.get("return_datetime_utc"),
    }


def get_planning_run(run_id: str, *, db_path: Path = DEFAULT_DB_PATH) -> dict:
    """Read the metrics, visit order and review for a saved software run ID."""
    if not _valid_id(run_id):
        return _invalid_id("run_id")
    run_id = run_id.strip()
    source = {
        "type": "sqlite_planning_snapshot",
        "tables": ["planning_runs", "planning_reviews"],
        "run_id": run_id,
    }
    try:
        run = get_run(db_path, run_id, read_only=True)
        if run is None:
            return _response(source=source, code="run_not_found", message=f"找不到规划运行 {run_id!r}。")
        review = get_review(db_path, run_id, read_only=True)
        if review is None:
            return _response(source=source, code="run_not_found", message=f"找不到规划运行 {run_id!r}。")
        data = {
            **run,
            "historical_metrics": _metrics(run["historical_metrics"]),
            "suggested_metrics": _metrics(run["suggested_metrics"]),
            "suggested_stops": run.get("suggested_stops"),
            "review": review,
            "units": {**_METRIC_UNITS, "solve_seconds": "seconds", "search_budget_seconds": "seconds"},
        }
    except (sqlite3.Error, OSError, KeyError, ValueError, TypeError):
        return _query_error(source)
    return _response(data, source={**source, "route_id": data["route_id"]})


def _difference(field: str, historical, suggested):
    if historical is None or suggested is None:
        return None
    if field in _RISK_METRICS:
        return suggested - historical
    return float(Decimal(str(suggested)) - Decimal(str(historical)))


def _assess(deltas: dict) -> dict:
    decision_fields = ("total_operation_seconds", *_RISK_METRICS)
    missing = [field for field in decision_fields if deltas[field] is None]
    increased = [field for field in _RISK_METRICS if deltas[field] is not None and deltas[field] > 0]
    if increased:
        code = "window_risk_increased"
        message = "建议路线的时间窗口风险增加。"
        if missing:
            message += "部分必要指标缺失，无法充分判断整体效果。"
    elif missing:
        code = "insufficient_data"
        message = "必要指标缺失，无法充分判断是否应替换历史路线。"
    elif deltas["total_operation_seconds"] < 0:
        code = "ready_for_human_review"
        message = "预计总作业时间减少且窗口风险未增加，可提交人工审核。"
    elif deltas["total_operation_seconds"] > 0 and all(deltas[field] == 0 for field in _RISK_METRICS):
        code = "no_basis_to_replace"
        message = "预计总作业时间增加，窗口风险未减少，当前没有充分依据建议替换历史路线。"
    else:
        code = "no_clear_replacement"
        message = "总作业时间未减少，请结合窗口风险的变化由人工判断。"
    return {"code": code, "message": message, "missing_metrics": missing, "increased_risk_metrics": increased}


def compare_planning_run(run_id: str, *, db_path: Path = DEFAULT_DB_PATH) -> dict:
    """Compare saved metrics. A positive delta means suggested minus historical increased."""
    result = get_planning_run(run_id, db_path=db_path)
    if not result["ok"]:
        return result

    run = result["data"]
    comparisons = {}
    for field in _COMPARE_METRICS:
        historical = (run["historical_metrics"] or {}).get(field)
        suggested = (run["suggested_metrics"] or {}).get(field)
        comparisons[field] = {
            "historical": historical,
            "suggested": suggested,
            "delta": _difference(field, historical, suggested),
            "unit": _METRIC_UNITS[field],
        }
    return _response({
        "run_id": run["run_id"],
        "route_id": run["route_id"],
        "review": run["review"],
        "delta_direction": "suggested_minus_historical",
        "metrics": comparisons,
        "assessment": _assess({field: values["delta"] for field, values in comparisons.items()}),
    }, source=result["source"])


_TOOLS = {
    "list_planning_runs": (list_planning_runs, "route_id", "按 Amazon Route ID 查询已保存的离线规划运行。"),
    "get_planning_run": (get_planning_run, "run_id", "按规划运行 ID 读取保存的指标、访问顺序及审核状态。"),
    "compare_planning_run": (compare_planning_run, "run_id", "比较保存的路线指标，给出差值和供人工参考的提示。"),
}
TOOL_DEFINITIONS = [
    {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {field: {
                "type": "string", "minLength": 1, "maxLength": 256, "pattern": r"\S",
                "description": "Amazon Route ID" if field == "route_id" else "软件生成的规划运行 ID",
            }},
            "required": [field],
            "additionalProperties": False,
        },
    }
    for name, (_, field, description) in _TOOLS.items()
]


def dispatch_tool(name: str, arguments: dict, *, db_path: Path = DEFAULT_DB_PATH) -> dict:
    """Call a whitelisted tool. The database path is trusted host configuration."""
    if not isinstance(name, str) or name not in _TOOLS:
        return _response(code="unknown_tool", message="该工具名称不在只读白名单中。")
    handler, field, _ = _TOOLS[name]
    if not isinstance(arguments, dict) or arguments.keys() != {field}:
        return _response(code="invalid_arguments", message=f"该工具只接受参数 {field}。")
    return handler(arguments[field], db_path=db_path)
