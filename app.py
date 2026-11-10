"""Streamlit workbench for offline single-vehicle delivery planning."""

import logging
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Error as SQLiteError
from time import perf_counter
from uuid import uuid4

import streamlit as st

from src.ai_assistant import answer_question, configured_model, redact_api_key, setup_message
from src.dashboard_charts import show_route_map, show_time_breakdown
from src.data_loader import load_route_and_sequence
from src.delivery_task import load_delivery_task
from src.planning_db import can_approve_run, get_review, get_run, list_runs, save_run, submit_review
from src.planning_report import export_planning_json, export_stop_order_csv, report_stops, snapshot_suggested_stops
from src.route_optimizer import optimize_route
from src.schedule_evaluator import evaluate_schedule


TITLE = "AI Last-Mile Delivery Planning Assistant"
DEFAULT_ROUTE_ID = "RouteID_00143bdd-0a6b-49ec-bb35-36593d303e77"
DATA_DIR = Path(__file__).resolve().parent / "data" / "raw"
DB_PATH = DATA_DIR.parent.parent / "outputs" / "planning_runs.sqlite3"
DATA_FILES = ("route_data.json", "actual_sequences.json", "travel_times.json", "package_data.json")
SEARCH_BUDGET_SECONDS = 20
SOLVER_PARAMETERS = {
    "vehicles": 1,
    "objective": "directed_driving_time",
    "first_solution_strategy": "PATH_CHEAPEST_ARC",
    "local_search_metaheuristic": "GUIDED_LOCAL_SEARCH",
    "cost_scale": 1000,
}
METRIC_FIELDS = (
    ("预计行驶时间（秒）", "driving_seconds", ",.1f"),
    ("包裹计划服务时间合计（秒）", "planned_service_time_seconds_sum", ",.1f"),
    ("预计等待时间（秒）", "waiting_seconds", ",.1f"),
    ("预计总作业时间（秒）", "total_operation_seconds", ",.1f"),
    ("窗口风险包裹数", "risk_package_count", "d"),
    ("窗口风险站点数", "risk_stop_count", "d"),
    ("窗口交集为空的站点数", "window_conflict_stop_count", "d"),
)
LOGGER = logging.getLogger(__name__)


def _show_header() -> None:
    st.html("""
        <style>
            .lm-hero {
                padding: clamp(1.2rem, 3vw, 2.25rem);
                border: 1px solid #DBE4F0;
                border-radius: 1rem;
                background: linear-gradient(110deg, #FFFFFF, #EAF1FF);
                color: #182B47;
            }
            .lm-hero .lm-kicker {
                display: flex; align-items: center; flex-wrap: wrap; gap: .65rem;
                color: #315284; font-size: .85rem; font-weight: 650; letter-spacing: .06em;
            }
            .lm-hero .lm-badge {
                border-radius: 2rem; padding: .25rem .65rem; background: #DDE8FF; color: #24458D;
            }
            .lm-hero h1 { margin: .65rem 0 .35rem; padding: 0; font-size: clamp(1.65rem, 3.4vw, 2.6rem); }
            .lm-hero p { margin: 0; color: #405773; }
        </style>
        <header class="lm-hero">
            <div class="lm-kicker">物流运营 / 单车配送 <span class="lm-badge">离线模拟</span></div>
            <h1>末端配送工作台</h1>
            <p>生成路线、比较作业时程、审核规划结果。页面指标均为离线模拟估计。</p>
        </header>
    """)


def _data_version(data_dir: Path, filenames: tuple[str, ...] = DATA_FILES) -> tuple:
    stamps = []
    for filename in filenames:
        stat = (data_dir / filename).stat()
        stamps.append((filename, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino))
    return tuple(stamps)


@st.cache_data(max_entries=4, ttl=900, show_spinner=False)
def _load_task(route_id: str, data_dir: Path, data_version: tuple) -> dict:
    task = load_delivery_task(route_id, data_dir)
    if _data_version(data_dir) != data_version:
        raise ValueError("加载期间原始文件发生变化，请重新加载。")
    return task


@st.cache_data(max_entries=4, ttl=900, show_spinner=False)
def _load_map_route(route_id: str, data_dir: Path, data_version: tuple) -> dict:
    return load_route_and_sequence(route_id, data_dir)


def _error_detail(error: Exception) -> str:
    if isinstance(error, FileNotFoundError):
        filename = Path(error.filename).name if error.filename else "原始数据文件"
        return f"找不到 {filename}，请检查文件路径。"
    if isinstance(error, (KeyError, ValueError, OSError, TypeError, RuntimeError, OverflowError, SQLiteError)):
        detail = str(error.args[0]) if isinstance(error, KeyError) and error.args else str(error)
        return next((line.strip() for line in detail.splitlines() if line.strip()), "操作返回了错误。")[:300]
    LOGGER.exception("Workbench operation failed")
    return "发生意外错误，请查看 Streamlit 服务日志。"


def _show_error(action: str, error: Exception) -> None:
    st.error(f"{action}。{_error_detail(error)}")


def _utc(timestamp: datetime | str) -> str:
    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(timestamp)
    return timestamp.isoformat(sep=" ", timespec="milliseconds").removesuffix("+00:00")


def _show_task(task: dict) -> None:
    st.subheader("配送任务")
    st.caption(f"当前路线 `{task['route_id']}`")
    metrics = (
        ("配送站 / 站点 ID", f"{task['station_code']} / {task['station_id']}"),
        ("配送日期", task["delivery_date"].isoformat()),
        ("出发时间（UTC）", task["departure_datetime_utc"].strftime("%H:%M:%S")),
        ("站点数量（含配送站）", task["stop_count"]),
        ("包裹记录数", sum(stop["package_count"] for stop in task["packages_by_stop"].values())),
    )
    for column, (label, value) in zip(st.columns(len(metrics)), metrics, strict=True):
        column.metric(label, value, border=True)


def _show_comparison(historical: dict, suggested: dict) -> None:
    st.subheader("历史与建议路线对比")
    st.caption("行驶与总作业时间均含返程。卡片显示建议路线指标。差值为建议减历史，百分比以历史值为基准。")
    cards = ("total_operation_seconds", "driving_seconds", "waiting_seconds", "risk_package_count")
    card_columns = dict(zip(cards, st.columns(len(cards)), strict=True)) if suggested else {}
    comparison = []
    for label, field, spec in METRIC_FIELDS:
        baseline, planned = historical.get(field), suggested.get(field)
        delta = planned - baseline if baseline is not None and planned is not None else None
        delta_text = format(delta, f"+{spec}" if delta else spec) if delta is not None else None
        planned_text = format(planned, spec) if planned is not None else "—"
        comparison.append({
            "指标": label,
            "历史路线": format(baseline, spec) if baseline is not None else "—",
            "建议路线": planned_text,
            "差值": delta_text if delta_text is not None else "—",
        })
        if field in card_columns:
            unit = "秒" if field.endswith("seconds") else "个"
            if delta_text is not None:
                delta_text = f"{delta_text} {unit}"
                if unit == "秒" and baseline > 0:
                    pct = delta / baseline
                    delta_text += f" ({pct:+.2%})" if pct else " (0.00%)"
            card_columns[field].metric(
                label, planned_text, delta=delta_text,
                delta_color="inverse" if delta else "off", delta_arrow="auto" if delta else "off", border=True,
            )

    if suggested:
        baseline_total = historical.get("total_operation_seconds")
        planned_total = suggested.get("total_operation_seconds")
        if baseline_total is not None and planned_total is not None and planned_total > baseline_total:
            st.warning(f"**总作业时间增加** · 建议路线比历史路线长 {planned_total - baseline_total:,.1f} 秒（含返程）。")
        risk_increases = []
        for label, field, _ in METRIC_FIELDS:
            if field not in ("risk_package_count", "risk_stop_count", "window_conflict_stop_count"):
                continue
            baseline, planned = historical.get(field), suggested.get(field)
            if baseline is not None and planned is not None and planned > baseline:
                risk_increases.append(f"{label}由 {baseline} 增至 {planned}")
        if risk_increases:
            st.warning(f"**模拟时间窗口风险增加** · {'；'.join(risk_increases)}。请在采用前人工复核。")
        show_time_breakdown(historical, suggested)
    comparison.append({
        "指标": "预计返回配送站（UTC）",
        "历史路线": _utc(historical["return_datetime_utc"]) if historical.get("return_datetime_utc") else "—",
        "建议路线": _utc(suggested["return_datetime_utc"]) if suggested.get("return_datetime_utc") else "—",
        "差值": "—",
    })
    with st.expander("查看全部指标", expanded=not bool(suggested)):
        st.caption("横线表示该次运行没有对应指标或差值。")
        st.table(comparison)


def _show_order(order: list[str]) -> None:
    with st.expander(f"建议访问顺序 · {len(order)} 个访问序号"):
        st.code(" → ".join(order), language=None, wrap_lines=True)


def _show_plan_source(record: dict, *, saved: bool) -> None:
    if saved:
        st.caption(f"方案来源 · 已保存方案 · run_id `{record['run_id']}`")
    else:
        st.caption("方案来源 · 当前会话方案（尚未保存）")


def _show_plan(plan: dict, task: dict) -> None:
    record = plan["record"]
    station = task["station_id"]
    last_record = st.session_state.get("last_record", {})
    saved = "save_error" not in st.session_state and last_record.get("run_id") == record["run_id"]
    _show_plan_source(record, saved=saved)
    status = f"{record['solver_status']}，求解耗时 {record['solve_seconds']:.3f} 秒"
    order = record["suggested_order"]
    if order is None:
        st.error(f"求解未找到可行路线。{status}。")
    else:
        st.info(f"已生成可行建议路线。{status}。请结合下方模拟指标判断是否改善，再由人工审核决定是否采用。")
    if record["error_message"]:
        st.error(record["error_message"])
    _show_comparison(record["historical_metrics"], record["suggested_metrics"] or {})
    show_route_map(task, order, key="current_route_map")
    if order is None:
        return
    _show_order(order)
    suggested = plan.get("suggested")
    if suggested is None:
        return
    rows = [{
        "顺序": 0, "站点": station, "事件": "出发", "预计离开（UTC）": _utc(suggested["departure_datetime_utc"]),
    }]
    for position, stop_id in enumerate(order[1:-1], start=1):
        stop = suggested["stop_details"][stop_id]
        rows.append({
            "顺序": position,
            "站点": stop_id,
            "事件": "投递",
            "预计到达（UTC）": _utc(stop["arrival_datetime_utc"]),
            "计划服务开始（UTC）": _utc(stop["service_start_datetime_utc"]),
            "预计离开（UTC）": _utc(stop["departure_datetime_utc"]),
            "入站行驶（秒）": stop["travel_seconds"],
            "等待（秒）": stop["waiting_seconds"],
            "包裹计划服务合计（秒）": stop["planned_service_time_seconds_sum"],
            "风险包裹数": len(stop["window_violations"]),
            "窗口提示": "窗口交集为空" if stop["has_empty_time_window_intersection"] else "",
        })
    rows.append({
        "顺序": len(order) - 1, "站点": station, "事件": "返回",
        "预计到达（UTC）": _utc(suggested["return_datetime_utc"]), "入站行驶（秒）": suggested["return_travel_seconds"],
    })
    with st.expander(f"建议路线逐站模拟 · {len(rows)} 行"):
        st.caption("以站点服务开始时刻检查包裹时间窗口。逐站时间均为 UTC。")
        st.dataframe(rows, hide_index=True, height=420, column_order=(
            "顺序", "站点", "事件", "预计到达（UTC）", "计划服务开始（UTC）", "预计离开（UTC）",
            "入站行驶（秒）", "等待（秒）", "包裹计划服务合计（秒）", "风险包裹数", "窗口提示",
        ) if suggested["stop_details"] else None)


def _snapshot(schedule: dict, driving_seconds: float | None) -> dict:
    metrics = {field: schedule[field] for _, field, _ in METRIC_FIELDS if field in schedule}
    if "driving_seconds" not in metrics and driving_seconds is not None:
        metrics["driving_seconds"] = driving_seconds
    if "return_datetime_utc" in schedule:
        metrics["return_datetime_utc"] = schedule["return_datetime_utc"].astimezone(UTC).isoformat()
    return metrics


def _run_plan(task: dict) -> dict:
    record = {
        "run_id": str(uuid4()),
        "route_id": task["route_id"],
        "started_at_utc": datetime.now(UTC).isoformat(timespec="microseconds"),
        "search_budget_seconds": SEARCH_BUDGET_SECONDS,
        "algorithm": "OR-Tools RoutingModel",
        "parameters": SOLVER_PARAMETERS,
    }
    plan = {"record": record}
    started = perf_counter()
    try:
        solver = optimize_route(task, time_limit_seconds=SEARCH_BUDGET_SECONDS)
        error_message = None
    except Exception as error:
        solver = {"status": "EXECUTION_ERROR", "solve_seconds": perf_counter() - started, "found_solution": False}
        error_message = f"求解异常。{_error_detail(error)}"

    order = solver.get("suggested_order") if solver["found_solution"] else None
    try:
        plan["historical"] = evaluate_schedule(task, task["ordered_stops"])
        if order is not None:
            plan["suggested"] = evaluate_schedule(task, order)
    except Exception as error:
        message = f"路线模拟失败。{_error_detail(error)}"
        error_message = f"{error_message} {message}" if error_message else message

    record.update(
        solver_status=solver["status"],
        solve_seconds=solver["solve_seconds"],
        suggested_order=order,
        historical_metrics=_snapshot(plan.get("historical", {}), solver.get("historical_driving_seconds")),
        suggested_metrics=_snapshot(plan.get("suggested", {}), solver.get("suggested_driving_seconds")) if order is not None else None,
        error_message=error_message,
    )
    if order is not None:
        record["suggested_stops"] = snapshot_suggested_stops(order, task["stops"], plan.get("suggested"))
    return plan


def _persist_run(record: dict) -> None:
    try:
        save_run(DB_PATH, record)
    except (SQLiteError, OSError, TypeError, ValueError, OverflowError) as error:
        st.session_state["save_error"] = _error_detail(error)
    else:
        st.session_state.pop("save_error", None)


def _show_save_control() -> None:
    record = st.session_state.get("last_record")
    if record is None:
        return
    with st.container(border=True):
        st.caption(f"本会话最近一次规划 · {record['route_id']} · {record['run_id']}")
        if st.button("保存本次规划", help="已保存的运行可以重复确认，写入失败时可在此重试。"):
            _persist_run(record)
        if error := st.session_state.get("save_error"):
            st.error(f"写入 SQLite 失败。{error} 请保存本次规划后再发起新求解。")
        else:
            st.success("本次规划已保存到 SQLite。可在「历史规划记录」中查看。")


def _show_workbench() -> None:
    _show_save_control()

    with st.container(border=True):
        st.subheader("1. 加载配送任务")
        st.caption("输入 Amazon Route ID，查看配送站、站点与包裹概况。")
        input_column, button_column = st.columns([4, 1], vertical_alignment="bottom")
        route_id = input_column.text_input("Route ID", value=DEFAULT_ROUTE_ID).strip()
        load_clicked = button_column.button("加载配送任务", type="primary", width="stretch", disabled=not route_id)

    if load_clicked:
        st.session_state.pop("loaded_task", None)
        st.session_state.pop("plan", None)
        try:
            with st.spinner("正在读取配送任务…", show_time=True):
                version = _data_version(DATA_DIR)
                task = _load_task(route_id, DATA_DIR, version)
            st.session_state["loaded_task"] = (task, version)
        except Exception as error:
            _show_error("加载配送任务失败", error)
            return

    loaded = st.session_state.get("loaded_task")
    if loaded is None:
        with st.container(border=True):
            st.markdown("**从一条路线开始**")
            st.caption("加载任务后，可生成建议路线并比较两条路线的作业时长和时间窗口风险。")
        return
    task, version = loaded
    try:
        if _data_version(DATA_DIR) != version:
            st.warning("原始数据文件已变化，请重新加载任务。")
            st.session_state.pop("loaded_task", None)
            st.session_state.pop("plan", None)
            return
    except OSError as error:
        _show_error("无法检查原始数据文件", error)
        st.session_state.pop("loaded_task", None)
        st.session_state.pop("plan", None)
        return
    if route_id != task["route_id"]:
        st.caption("请点击「加载配送任务」切换到输入的路线。")
        return

    _show_task(task)
    with st.container(border=True):
        description, action = st.columns([4, 1], vertical_alignment="bottom")
        description.subheader("2. 生成建议路线")
        description.caption(f"单车从配送站出发并返回配送站。求解只优化预计行驶时间，搜索预算为 {SEARCH_BUDGET_SECONDS} 秒。")
        optimize_clicked = action.button(
            "生成建议路线", type="primary", width="stretch", disabled="save_error" in st.session_state,
        )
    if optimize_clicked:
        with st.spinner("正在运行 OR-Tools 并模拟路线，请稍候…", show_time=True):
            plan = _run_plan(task)
            st.session_state["plan"] = plan
            st.session_state["last_record"] = plan["record"]
            _persist_run(plan["record"])
        st.rerun()

    plan = st.session_state.get("plan")
    if plan is not None and "record" not in plan:
        st.session_state.pop("plan", None)
        st.info("升级前的会话结果没有运行记录。请生成一次新规划以保存结果。")
    elif plan is not None:
        _show_plan(plan, task)
    else:
        show_route_map(task, None, key="loaded_route_map")


def _show_history() -> str | None:
    search_panel = st.container(border=True)
    search_panel.subheader("查找历史规划")
    search_panel.caption("按 Amazon Route ID 查找路线，再按规划运行 ID 选择保存的本地模拟快照。")
    route_id = search_panel.text_input("历史查询 Amazon Route ID", value=DEFAULT_ROUTE_ID, key="history_route_id").strip()
    if not route_id:
        return
    try:
        runs = list_runs(DB_PATH, route_id)
        if not runs:
            st.info("该路线暂无已保存的规划记录。")
            return
        labels = {
            run["run_id"]: f"{run['started_at_utc']} · {run['solver_status']} · {run['run_id']}"
            for run in runs
        }
        run_id = search_panel.selectbox("选择历史记录", list(labels), index=None, format_func=labels.__getitem__)
        if run_id is None:
            search_panel.caption(f"找到 {len(runs)} 条规划记录。选择一条查看指标、审核及导出。")
            return
        record = get_run(DB_PATH, run_id)
        review = get_review(DB_PATH, run_id)
        if record is None or review is None or record["route_id"] != route_id:
            st.warning("该记录已不可用，请重新选择。")
            return
    except (SQLiteError, OSError, ValueError) as error:
        _show_error("读取 SQLite 规划记录失败", error)
        return

    _show_plan_source(record, saved=True)
    with st.expander("规划运行信息"):
        st.caption(f"规划运行 ID（run_id） `{record['run_id']}` · {record['started_at_utc']} · Amazon Route ID `{record['route_id']}`")
        st.write(f"求解状态 `{record['solver_status']}` · 求解耗时 {record['solve_seconds']:.3f} 秒 · 搜索预算 {record['search_budget_seconds']:g} 秒")
        st.caption("求解状态仅描述求解结果。业务改善请看模拟对比，批准或驳回请看人工审核记录。")
        st.caption(record["algorithm"] + " · " + " · ".join(f"{key}={value}" for key, value in record["parameters"].items()))
    if record["error_message"]:
        st.error(record["error_message"])
    _show_comparison(record["historical_metrics"], record["suggested_metrics"] or {})
    if record["suggested_order"] is not None:
        _show_order(record["suggested_order"])
    else:
        st.info("本次运行没有可保存的建议访问顺序。")
    try:
        version = _data_version(DATA_DIR, ("route_data.json", "actual_sequences.json"))
        route = _load_map_route(record["route_id"], DATA_DIR, version)
    except (OSError, KeyError, ValueError) as error:
        st.warning(f"无法读取历史方案的站点坐标或访问顺序。{_error_detail(error)}")
    else:
        show_route_map(route, record["suggested_order"], key=f"saved_route_map_{run_id}", saved=True)
    review_column, export_column = st.columns([1.2, 1])
    with review_column.container(border=True):
        _show_review(record, review)
    with export_column.container(border=True):
        _show_exports(record, review)
    return record["run_id"]


def _show_review(record: dict, review: dict) -> None:
    st.subheader("人工审核")
    decision = review["decision"]
    status = {"PENDING": ":orange-badge[待审核]", "APPROVED": ":green-badge[已批准]", "REJECTED": ":red-badge[已驳回]"}
    st.markdown(f"{status[decision]} · `{decision}`")
    st.caption("本地单操作员演示。每次运行只记录一次最终决定。")
    if decision != "PENDING":
        st.caption(f"审核时间（UTC） {review['reviewed_at_utc']}")
        if review["note"]:
            st.text(review["note"])
        return

    can_approve = can_approve_run(record)
    if not can_approve:
        st.info("本次运行没有可批准的有效建议路线。")
    with st.form(f"review_{record['run_id']}", enter_to_submit=False):
        note = st.text_area("审核备注（驳回时必填）")
        approved = st.form_submit_button("批准方案", disabled=not can_approve)
        rejected = st.form_submit_button("驳回方案")
    if approved or rejected:
        try:
            submit_review(DB_PATH, record["run_id"], "APPROVED" if approved else "REJECTED", note)
        except (SQLiteError, OSError, KeyError, ValueError) as error:
            _show_error("保存审核失败", error)
        else:
            st.rerun()


def _show_exports(record: dict, review: dict) -> None:
    st.subheader("导出规划结果")
    _, source = report_stops(record)
    if source in ("not_saved", "return_time_only"):
        st.caption("该运行缺少逐站时程快照。导出保留已有时刻，缺失时刻在 JSON 中为 null、在 CSV 中留空。")
    try:
        report = export_planning_json(record, review)
        stop_csv = export_stop_order_csv(record)
    except (TypeError, ValueError) as error:
        _show_error("生成导出文件失败", error)
        return
    run_id = record["run_id"]
    json_column, csv_column = st.columns(2)
    json_column.download_button(
        "导出规划报告 JSON", report.encode("utf-8"), file_name=f"planning-{run_id}.json",
        mime="application/json", on_click="ignore", key=f"export_json_{run_id}",
    )
    csv_column.download_button(
        "导出站点顺序 CSV", stop_csv.encode("utf-8"), file_name=f"stops-{run_id}.csv",
        mime="text/csv", on_click="ignore", key=f"export_csv_{run_id}", disabled=source == "no_suggested_route",
    )


def _show_chat_reply(response: dict) -> None:
    with st.chat_message("assistant"):
        if response["ok"]:
            st.markdown(response["answer"])
        else:
            st.error(response["error"]["message"])
        calls = response["tool_calls"]
        if calls:
            with st.expander(f"工具调用记录 · {len(calls)} 次"):
                for call in calls:
                    query = call["query"]
                    target = "，".join(f"{field}={value}" for field, value in query.items()) if query else "未提交有效查询对象"
                    status = "成功" if call["ok"] else call["error_code"]
                    if "result_count" in call:
                        status = f"成功（返回 {call['result_count']} 条规划记录）"
                    elif call["name"] == "get_planning_run" and call.get("result_route_id"):
                        status = "成功（已返回规划详情）"
                    if call.get("origin") == "evidence_fallback":
                        status += " · 证据补查"
                    st.text(f"工具 {call['name']} · {target} · {status}")


def _show_ai_assistant(selected_run_id: str | None) -> None:
    setup = setup_message()
    model = configured_model()
    label = f"智谱模型 `{model}` · " if model else ""
    with st.container(border=True):
        st.subheader("AI运营助手")
        st.caption(f"{label}只读查询保存的规划快照。配送指标均为离线模拟估计。")
        if selected_run_id:
            st.caption(f"当前默认查询对象（run_id） `{selected_run_id}`。也可在问题中指定其他 ID。")
        else:
            st.caption("请在问题中提供 Amazon Route ID 或 run_id，也可先在历史规划记录中选择一次运行。")

    turns = st.session_state.setdefault("ai_chat_turns", [])
    for turn in turns:
        with st.chat_message("user"):
            st.text(turn["question"])
        _show_chat_reply(turn["response"])

    if setup:
        st.info(setup)
    elif not turns:
        st.markdown("**可以这样问**")
        st.caption("这次规划的行驶时间、等待时间和总作业时间分别变化了多少？")
    question = st.chat_input("询问规划指标、审核状态或路线差异", disabled=setup is not None, max_chars=2000)
    if question and question.strip():
        question = redact_api_key(question.strip())
        history = []
        for turn in reversed(turns[-6:]):
            if "selected_run_id" not in turn or turn["selected_run_id"] != selected_run_id:
                break
            if turn["response"]["ok"]:
                history[:0] = [
                    {"role": "user", "content": turn["question"], "run_id": turn["response"].get("run_id")},
                    {"role": "assistant", "content": turn["response"]["answer"]},
                ]
        with st.chat_message("user"):
            st.text(question)
        with st.spinner("正在查询并生成回答…"):
            response = answer_question(question, history=history, selected_run_id=selected_run_id, db_path=DB_PATH)
        turns.append({"question": question, "selected_run_id": selected_run_id, "response": response})
        _show_chat_reply(response)


def main() -> None:
    st.set_page_config(page_title=TITLE, page_icon="🚚", layout="wide")
    _show_header()
    workbench, history, assistant = st.tabs(["当前规划", "历史规划记录", "AI运营助手"])
    with workbench:
        _show_workbench()
    with history:
        selected_run_id = _show_history()
    with assistant:
        _show_ai_assistant(selected_run_id)


if __name__ == "__main__":
    main()
