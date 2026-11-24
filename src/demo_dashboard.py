"""Read-only dashboard for the verified D1 demonstration package."""

from pathlib import Path

import pandas as pd
import streamlit as st

from evals.validate_demo_data import DEFAULT_PACKAGE, load_package
from .dashboard_charts import show_time_breakdown
from . import demo_portfolio as portfolio


CASE_LABELS = {"A": "A. 效率改善", "B": "B. 效率权衡", "C": "C. 时间窗口风险"}
FIELDS = (
    ("预计总作业时间", "total_operation_seconds", ",.1f", "秒"),
    ("预计行驶时间", "driving_seconds", ",.1f", "秒"),
    ("包裹计划服务时间合计", "planned_service_time_seconds_sum", ",.1f", "秒"),
    ("预计等待时间", "waiting_seconds", ",.1f", "秒"),
    ("模拟窗口风险包裹数", "risk_package_count", "d", "个"),
    ("模拟窗口风险站点数", "risk_stop_count", "d", "个"),
)


def has_local_data(data_dir: Path, filenames: tuple[str, ...]) -> bool:
    return all((data_dir / filename).is_file() for filename in filenames)


def display(value, spec):
    return "未提供" if value is None else format(value, spec)


def show_case(case: dict) -> None:
    st.subheader(case["description"])
    task = case["task"]
    st.caption(f"{display(task.get('stop_count'), 'd')} 个站点（含配送站） · "
               f"{display(task.get('package_count'), 'd')} 条包裹记录 · "
               f"{display(task.get('valid_time_window_count'), 'd')} 个有效时间窗口")
    comparisons = case["comparisons"]
    cards = (FIELDS[0], FIELDS[1], FIELDS[3], FIELDS[4])
    with st.container(key="route_kpis"):
        for col, (label, field, spec, unit) in zip(st.columns(len(cards)), cards, strict=True):
            values = comparisons[field]
            delta = values["delta"]
            delta_spec = "+,.1f" if unit == "秒" else "+d"
            delta_value = None if delta is None else delta if unit == "秒" else int(delta)
            col.metric(f"{label}（{unit}）", display(values["suggested"], spec),
                       delta=None if delta_value is None else f"{delta_value:{delta_spec}} {unit}",
                       delta_color="inverse" if delta else "off", delta_arrow="auto" if delta else "off", border=True)
    st.caption("卡片显示建议值与差值（建议 − 历史）。")
    total_delta = comparisons["total_operation_seconds"]["delta"]
    if total_delta is not None and total_delta > 0:
        st.warning(f"预计总作业时间增加 {total_delta:,.1f} 秒。等待和服务时间也会影响总作业，行驶更短的方案仍需核对。")
    elif total_delta is not None and total_delta < 0:
        st.success(f"预计总作业时间减少 {abs(total_delta):,.1f} 秒。")
    new_risk = case["new_risk"]
    if any(value is not None and value > 0 for value in new_risk.values()):
        st.warning(f"新增模拟时间窗口风险 · {display(new_risk['package_count'], 'd')} 个包裹，"
                   f"{display(new_risk['stop_count'], 'd')} 个站点。请人工复核受影响的配送承诺。")
    else:
        st.caption(f"新增模拟风险包裹数 · {display(new_risk['package_count'], 'd')}，"
                   f"新增模拟风险站点数 · {display(new_risk['stop_count'], 'd')}。")

    time_fields = ("driving_seconds", "planned_service_time_seconds_sum", "waiting_seconds", "total_operation_seconds")
    if all(case[side].get(field) is not None for side in ("historical", "suggested") for field in time_fields):
        show_time_breakdown(case["historical"], case["suggested"], show_totals=False)
    else:
        st.info("部分时间指标未提供，完整时间构成图暂不可用。")
    rows = []
    for label, field, spec, unit in FIELDS:
        values = comparisons[field]
        change_percent = values["change_percent"]
        improvement = None if change_percent is None else 0.0 if change_percent == 0 else -change_percent
        rows.append({"指标": f"{label}（{unit}）", "历史": display(values["historical"], spec),
                     "建议": display(values["suggested"], spec), "差值": display(values["delta"], "+,.1f"),
                     "改善（%）": display(improvement, "+.2f" if improvement else ".2f")})
    with st.expander("全部指标与改善百分比", expanded=True):
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.caption("改善百分比 =（历史 − 建议）÷ 历史。正值表示减少，历史基数为零时显示「未提供」。")
    if case["review_status"] is not None:
        st.info(f"原有历史快照审核状态 · {case['review_status']}。该决定来自保存记录。")
    with st.expander("数据来源与模拟口径"):
        source = "已保存规划快照" if case["source_type"] == "saved_planning_snapshot" else "M6-4 离线评估结果"
        st.caption(f"来源 · {source}")
        st.text(f"Amazon Route ID · {case['route_id']}")
        if case["run_id"] is not None:
            st.text(f"历史 run_id · {case['run_id']}")
        st.write(f"{case['algorithm']['name']} · 原搜索预算 {case['algorithm']['search_budget_seconds']:g} 秒")
        st.caption("总作业含返回配送站。服务时长为包裹计划服务合计，窗口风险以模拟服务开始时间检查。")


def show_analysis(package_dir: Path) -> None:
    st.title("配送方案分析")
    st.caption("三个精选案例 · 根据真实历史数据预计算 · 时长与风险均为离线模拟估计")
    try:
        cases = load_package(package_dir)
    except (OSError, ValueError, KeyError, TypeError):
        st.error("演示数据包缺失或校验失败，请检查 demo_data 文件及验证结果。")
        return
    selected = st.radio("选择演示案例", tuple(CASE_LABELS), format_func=CASE_LABELS.__getitem__, horizontal=True)
    show_case(next(case for case in cases if case["demo_case_id"] == selected))


def show_demo(package_dir: Path = DEFAULT_PACKAGE) -> None:
    page = portfolio.show_shell()
    if page == "配送方案分析":
        show_analysis(package_dir)
    else:
        try:
            evidence = portfolio.read_json(portfolio.EVIDENCE_PATH)
        except (OSError, ValueError):
            st.error("公开验收资料无法读取。请检查 evals/portfolio_evidence.json。")
            return
        if page == "项目总览":
            portfolio.show_overview(evidence["route"])
        elif page == "AI运营助手":
            portfolio.show_ai(evidence)
        else:
            portfolio.show_evaluation(evidence)
    portfolio.show_attribution()
