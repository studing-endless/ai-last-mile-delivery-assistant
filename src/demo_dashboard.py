"""Read-only dashboard for the verified D1 demonstration package."""

from pathlib import Path

import streamlit as st

from evals.validate_demo_data import DEFAULT_PACKAGE, load_package
from .dashboard_charts import show_time_breakdown


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
    st.caption("精选演示案例 · 已保存的预计算结果 · 所有时间与风险均为离线模拟估计")
    for col, label, field in zip(st.columns(3), ("站点数（含配送站）", "包裹记录数", "有效时间窗口数"),
                                  ("stop_count", "package_count", "valid_time_window_count"), strict=True):
        col.metric(label, display(case["task"].get(field), "d"), border=True)
    st.text(f"Amazon Route ID · {case['route_id']}")
    source = "已保存规划快照" if case["source_type"] == "saved_planning_snapshot" else "M6-4 离线评估结果"
    st.caption(f"来源 · {source}")
    if case["run_id"] is not None:
        st.text(f"历史 run_id · {case['run_id']}")
    if case["review_status"] is not None:
        st.info(f"原有历史快照审核状态 · {case['review_status']}。该决定来自保存记录。")

    comparisons = case["comparisons"]
    total_delta = comparisons["total_operation_seconds"]["delta"]
    if total_delta is not None and total_delta > 0:
        st.warning(f"预计总作业时间增加 {total_delta:,.1f} 秒。请结合等待时间判断行驶节省。")
    elif total_delta is not None and total_delta < 0:
        st.success(f"预计总作业时间减少 {abs(total_delta):,.1f} 秒。")
    new_risk = case["new_risk"]
    if any(value is not None and value > 0 for value in new_risk.values()):
        st.warning(f"新增模拟时间窗口风险 · {display(new_risk['package_count'], 'd')} 个包裹，"
                   f"{display(new_risk['stop_count'], 'd')} 个站点。需要结合风险判断方案。")
    else:
        st.caption(f"新增模拟风险包裹数 · {display(new_risk['package_count'], 'd')}，"
                   f"新增模拟风险站点数 · {display(new_risk['stop_count'], 'd')}。")

    st.subheader("历史与建议路线对比")
    st.caption("卡片显示建议值。差值为建议－历史。改善百分比为（历史－建议）÷历史，正值表示减少。")
    cards = (FIELDS[0], FIELDS[1], FIELDS[3], FIELDS[4])
    for start in (0, 2):
        for col, (label, field, spec, unit) in zip(st.columns(2), cards[start:start + 2], strict=True):
            values = comparisons[field]
            delta = values["delta"]
            col.metric(f"{label}（{unit}）", display(values["suggested"], spec),
                       delta=None if delta is None else f"{delta:+,.1f} {unit}",
                       delta_color="inverse" if delta else "off", delta_arrow="auto" if delta else "off", border=True)
    time_fields = ("driving_seconds", "planned_service_time_seconds_sum", "waiting_seconds", "total_operation_seconds")
    if all(case[side].get(field) is not None for side in ("historical", "suggested") for field in time_fields):
        show_time_breakdown(case["historical"], case["suggested"])
    else:
        st.info("部分时间指标未提供，完整时间构成图暂不可用。")
    rows = []
    for label, field, spec, unit in FIELDS:
        values = comparisons[field]
        percent = values["change_percent"]
        rows.append({"指标": f"{label}（{unit}）", "历史": display(values["historical"], spec),
                     "建议": display(values["suggested"], spec), "差值": display(values["delta"], "+,.1f"),
                     "改善（%）": display(-percent if percent is not None else None, "+.2f")})
    with st.expander("全部指标与改善百分比", expanded=True):
        st.dataframe(rows, hide_index=True, width="stretch")
    st.caption("总作业时间含返回配送站。服务时间为包裹级计划服务合计。风险按模拟服务开始时间检查包裹窗口。")
    with st.expander("数据来源与模拟口径"):
        st.write(f"{case['algorithm']['name']} · 原搜索预算 {case['algorithm']['search_budget_seconds']:g} 秒")
        st.caption("原求解只优化行驶时间，时间窗口属于后验模拟。")
        for source_file in case["provenance"]["files"]:
            st.text(source_file["path"])


def show_demo(package_dir: Path = DEFAULT_PACKAGE) -> None:
    st.caption("在线只读演示模式")
    st.info("浏览三个精选案例的预计算离线模拟结果。完整 12 条路线统计见项目公开评估报告。")
    try:
        cases = load_package(package_dir)
    except (OSError, ValueError, KeyError, TypeError):
        st.error("演示数据包缺失或校验失败，请检查 demo_data 文件及验证结果。")
        return
    selected = st.radio("选择演示案例", tuple(CASE_LABELS), format_func=CASE_LABELS.__getitem__, horizontal=True)
    show_case(next(case for case in cases if case["demo_case_id"] == selected))
    st.subheader("AI 运营助手")
    st.caption("真实AI对话仅在本地完整版提供；历史调用演示后续补充。")
    st.caption("数据来源：[Amazon Last Mile Routing Research Challenge Dataset]"
               "(https://registry.opendata.aws/amazon-last-mile-challenges/) · Copyright Amazon.com, Inc. or its affiliates · "
               "[CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/) · 汇总和呈现方式已作修改。")
