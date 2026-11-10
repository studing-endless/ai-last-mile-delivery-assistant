"""Presentation charts for the Streamlit planning workbench."""

import altair as alt
import streamlit as st


HISTORICAL = "历史路线"
SUGGESTED = "建议路线"
ROUTES = (HISTORICAL, SUGGESTED)
TIME_COMPONENTS = (
    ("行驶时间", "driving_seconds"),
    ("包裹级计划服务合计", "planned_service_time_seconds_sum"),
    ("预计等待时间", "waiting_seconds"),
)


def show_time_breakdown(historical: dict, suggested: dict) -> None:
    with st.container(border=True):
        st.markdown("**作业时长构成**")
        baseline = historical.get("total_operation_seconds")
        planned = suggested.get("total_operation_seconds")
        delta = planned - baseline if baseline is not None and planned is not None else None
        totals = (
            ("历史预计总作业", baseline, ",.2f"),
            ("建议预计总作业", planned, ",.2f"),
            ("总作业差值（建议 − 历史）", delta, "+,.2f" if delta else ",.2f"),
        )
        for column, (label, seconds, spec) in zip(st.columns(3), totals, strict=True):
            column.metric(f"{label}（分钟）", format(seconds / 60, spec) if seconds is not None else "—")

        routes = ((HISTORICAL, historical), (SUGGESTED, suggested))
        if any(metrics.get(field) is None for _, metrics in routes for _, field in TIME_COMPONENTS):
            st.caption("缺少一项或多项时长，暂无法绘制完整的双路线对比图。")
            return
        rows = [
            {"路线": route, "时长构成": name, "顺序": index, "分钟": metrics[field] / 60, "原始秒数": str(metrics[field])}
            for route, metrics in routes
            for index, (name, field) in enumerate(TIME_COMPONENTS)
        ]
        chart = alt.Chart(alt.Data(values=rows)).mark_bar(size=28).encode(
            x=alt.X("分钟:Q", title="预计作业时长（分钟）", stack="zero"),
            y=alt.Y("路线:N", title=None, sort=list(ROUTES)),
            color=alt.Color(
                "时长构成:N", title=None,
                scale=alt.Scale(
                    domain=[name for name, _ in TIME_COMPONENTS],
                    range=["#315DC3", "#8EAAE4", "#D49A42"],
                ),
                legend=alt.Legend(orient="bottom", columns=1, labelLimit=0),
            ),
            order=alt.Order("顺序:Q"),
            tooltip=["路线:N", "时长构成:N", alt.Tooltip("分钟:Q", format=",.6f"), "原始秒数:N"],
        ).properties(height=100)
        st.altair_chart(chart, width="stretch", alt="历史与建议路线的作业时长构成（分钟）")


def show_route_map(route: dict, suggested_order: list[str] | None, *, key: str, saved: bool = False) -> None:
    """Draw only adjacent visits with valid coordinates at both endpoints."""
    historical_order = list(route["ordered_stops"])
    depot = historical_order[0]
    if historical_order[-1] != depot:
        historical_order.append(depot)
    orders = {HISTORICAL: historical_order}
    if suggested_order:
        orders[SUGGESTED] = suggested_order

    with st.container(border=True):
        st.markdown("**站点与路线示意**")
        st.caption("直线连接站点，表示访问顺序。实际道路轨迹未绘制，预计耗时来自原始交通时间矩阵。访问序号从 0 开始。")
        if saved:
            st.caption("站点坐标和历史访问顺序来自当前 Amazon 原始数据，建议顺序来自该 run_id 的保存快照。")
        options = [*orders, "同时显示"] if suggested_order else list(orders)
        selected = st.radio("显示路线", options, index=len(options) - 1, horizontal=True, key=key)
        visible = orders if selected == "同时显示" else {selected: orders[selected]}

        coordinates = {}
        for stop_id, stop in route["stops"].items():
            lat, lng = stop.get("lat"), stop.get("lng")
            if type(lat) in (int, float) and type(lng) in (int, float) and -90 <= lat <= 90 and -180 <= lng <= 180:
                coordinates[stop_id] = (lng, lat)
        if not coordinates:
            st.warning("该路线缺少有效站点坐标，无法绘制示意图。")
            return

        missing = (route["stops"].keys() | {stop for order in visible.values() for stop in order}) - coordinates.keys()
        if missing:
            st.warning(f"{len(missing)} 个站点缺少有效坐标。已略去这些站点及与它们相邻的连线。")

        positions = {name: {} for name in visible}
        segments = []
        for name, order in visible.items():
            for index, stop in enumerate(order):
                positions[name].setdefault(stop, []).append(str(index))
            for start, end in zip(order, order[1:]):
                if start in coordinates and end in coordinates:
                    lng, lat = coordinates[start]
                    end_lng, end_lat = coordinates[end]
                    segments.append({"路线": name, "经度": lng, "纬度": lat, "终点经度": end_lng, "终点纬度": end_lat})

        points = [
            {
                "站点 ID": stop, "站点类型": "配送站" if stop == depot else "投递点", "经度": lng, "纬度": lat,
                **{f"{name}序号": "、".join(indices.get(stop, [])) or "—" for name, indices in positions.items()},
            }
            for stop, (lng, lat) in coordinates.items()
        ]
        x = alt.X("经度:Q", title="经度", scale=alt.Scale(zero=False, nice=False, padding=14), axis=alt.Axis(format=".3f"))
        y = alt.Y("纬度:Q", title="纬度", scale=alt.Scale(zero=False, nice=False, padding=14), axis=alt.Axis(format=".3f"))
        colors = {HISTORICAL: "#546B88", SUGGESTED: "#2765D4"}
        dashes = {HISTORICAL: [4, 3], SUGGESTED: [1, 0]}
        lines = alt.Chart(alt.Data(values=segments)).mark_rule(strokeWidth=1.6, opacity=0.66).encode(
            x=x, y=y, x2="终点经度:Q", y2="终点纬度:Q",
            color=alt.Color(
                "路线:N", scale=alt.Scale(domain=list(visible), range=[colors[name] for name in visible]),
                legend=alt.Legend(orient="bottom"),
            ),
            strokeDash=alt.StrokeDash("路线:N", scale=alt.Scale(domain=list(visible), range=[dashes[name] for name in visible])),
        )
        stops = alt.Chart(alt.Data(values=points)).mark_point(filled=True, stroke="white", strokeWidth=1).encode(
            x=x, y=y,
            shape=alt.Shape("站点类型:N", scale=alt.Scale(domain=["配送站", "投递点"], range=["diamond", "circle"])),
            fill=alt.Fill("站点类型:N", scale=alt.Scale(domain=["配送站", "投递点"], range=["#AE5E13", "#43546E"]), legend=None),
            size=alt.Size("站点类型:N", scale=alt.Scale(domain=["配送站", "投递点"], range=[170, 32]), legend=None),
            tooltip=["站点 ID:N", "站点类型:N", *[f"{name}序号:N" for name in visible]],
        )
        chart = alt.layer(lines, stops).properties(height=360)
        st.altair_chart(chart, width="stretch", alt="配送站、投递点与按访问顺序连接的路线示意图")
        with st.expander("查看站点及访问序号"):
            st.dataframe(points, hide_index=True)
