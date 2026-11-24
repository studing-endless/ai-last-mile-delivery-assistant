"""Project overview, historical AI evidence and evaluation for the public demo."""

from html import escape
from pathlib import Path

import streamlit as st

from evals.validate_demo_data import read_json


TITLE = "AI末端配送规划与运营决策助手"
ENGLISH_TITLE = "AI Last-Mile Delivery Planning Assistant"
PAGES = ("项目总览", "配送方案分析", "AI运营助手", "项目评估")
EVIDENCE_PATH = Path(__file__).resolve().parents[1] / "evals" / "portfolio_evidence.json"
REPORT_URL = "https://github.com/studing-endless/ai-last-mile-delivery-assistant/blob/main/evals/results.md"
METRIC_NAMES = {
    "driving_seconds": "预计行驶时间",
    "waiting_seconds": "预计等待时间",
    "total_operation_seconds": "预计总作业时间",
    "planned_service_time_seconds_sum": "包裹计划服务时间合计",
}


def show_shell() -> str:
    st.html("""
        <style>
          .stMainBlockContainer:has(.d4-brand) {max-width: 78rem; padding-top: 4.5rem; padding-bottom: 1.25rem}
          .d4-brand {display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between;
            gap:.4rem 1rem; margin:.1rem 0 .2rem; color:#142b49; font-size:.94rem; font-weight:700}
          .d4-mode {background:#eaf0fa; color:#24436b; padding:.25rem .65rem;
            border-radius:100px; font-size:.76rem}
          .d4-hero {background:linear-gradient(115deg,#102642,#1b3d68); border-radius:1rem;
            padding:clamp(1.2rem,2.8vw,2.3rem); color:#fff}
          .d4-hero h1 {margin:.25rem 0 .6rem; padding:0; color:#fff;
            font-size:clamp(1.75rem,4vw,3.1rem); line-height:1.16; letter-spacing:.005em}
          .d4-hero p {max-width:53rem; margin:.4rem 0 0; color:#dce7f6}
          .d4-hero .d4-en {font-size:.83rem; color:#c1d4ef}
          .d4-flow {display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:.7rem}
          .d4-branches {display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:.7rem}
          .d4-node {background:#f1f5fb; border:1px solid #dce6f4; border-radius:.7rem;
            padding:.65rem .8rem; min-width:0; overflow-wrap:anywhere}
          .d4-node strong {display:block; color:#163052; font-size:.94rem}
          .d4-node small {display:block; color:#465b77; font-size:.81rem}
          .d4-node.d4-hub {background:#1a375b; border-color:#1a375b}
          .d4-hub strong,.d4-hub small {color:#fff}
          .d4-arrow {text-align:center; font-size:.77rem; color:#475f7c; padding:.4rem}
          .d4-turn-question {margin:.15rem 0; padding:.65rem .8rem; background:#edf3fb;
            border-left:3px solid #3867bb; border-radius:.35rem; overflow-wrap:anywhere}
          .st-key-portfolio_nav [role="radiogroup"] {gap:.4rem; flex-wrap:wrap}
          .st-key-portfolio_nav [role="radiogroup"] label {border:1px solid #d8e3f1; border-radius:.55rem;
            background:#fff; padding:.42rem .7rem; margin:0}
          .st-key-portfolio_nav [role="radiogroup"] label:has(input:checked) {background:#163052; border-color:#163052}
          .st-key-portfolio_nav [role="radiogroup"] label:has(input:checked) p {color:#fff; font-weight:650}
          .st-key-portfolio_nav [role="radiogroup"] label:focus-within {outline:2px solid #648be0; outline-offset:2px}
          @media(max-width:620px) {
            .d4-flow,.d4-branches {grid-template-columns:1fr 1fr}
            .st-key-overview_metrics [data-testid="stHorizontalBlock"],
            .st-key-route_kpis [data-testid="stHorizontalBlock"],
            .st-key-evaluation_metrics [data-testid="stHorizontalBlock"] {
              display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:.5rem;
            }
            .st-key-overview_metrics [data-testid="stColumn"],
            .st-key-route_kpis [data-testid="stColumn"],
            .st-key-evaluation_metrics [data-testid="stColumn"] {width:100%!important; min-width:0!important}
            .st-key-overview_metrics [data-testid="stMetricValue"],
            .st-key-route_kpis [data-testid="stMetricValue"],
            .st-key-evaluation_metrics [data-testid="stMetricValue"] {font-size:1.3rem}
          }
        </style>
        <div class="d4-brand">
          <span>AI末端配送规划与运营决策助手</span>
          <span class="d4-mode">在线只读演示模式</span>
        </div>
    """)
    return st.radio("页面导航", PAGES, horizontal=True, label_visibility="collapsed", key="portfolio_nav")


def show_metrics(items: tuple) -> None:
    for column, (label, value) in zip(st.columns(len(items)), items, strict=True):
        column.metric(label, value, border=True)


def show_overview(routes: dict) -> None:
    st.html(f"""
        <header class="d4-hero">
          <div class="d4-en">{ENGLISH_TITLE}</div>
          <h1>{TITLE}</h1>
          <p>面向配送规划员和站点运营人员。比较历史路线与优化建议，判断行驶节省是否会被等待抵消，
          并识别需要人工复核的配送时间窗口风险。</p>
        </header>
    """)
    with st.container(key="overview_metrics"):
        show_metrics((
            ("历史路线已索引", f"{routes['indexed']:,} 条"),
            ("评估路线求解成功", f"{routes['solved']}/{routes['evaluated']}"),
            ("平均预计行驶降幅", f"{routes['driving_mean_improvement_percent']:.2f}%"),
            ("平均预计总作业降幅", f"{routes['total_mean_improvement_percent']:.2f}%"),
        ))
    st.caption("训练集索引表示数据覆盖规模。求解与两项平均降幅均来自固定 12 条样本的离线模拟。")
    st.subheader("系统架构")
    st.html("""
        <div aria-label="本地完整系统架构">
          <div class="d4-flow">
            <div class="d4-node"><strong>Amazon 公开历史数据</strong><small>路线、包裹、有向行驶时间</small></div>
            <div class="d4-node"><strong>→ Python 数据处理</strong><small>校验、流式提取、组装任务</small></div>
            <div class="d4-node"><strong>→ OR-Tools</strong><small>求解单车闭环建议路线</small></div>
            <div class="d4-node"><strong>→ 配送时程模拟</strong><small>对比历史与建议的作业时间和窗口风险</small></div>
          </div>
          <div class="d4-arrow">↓ 保存规划结果与指标</div>
          <div class="d4-node d4-hub"><strong>SQLite 规划快照 ↔ Streamlit 工作台</strong>
            <small>查询历史方案、比较指标、导出结果</small></div>
          <div class="d4-arrow">↓ 两条决策路径</div>
          <div class="d4-branches">
            <div class="d4-node"><strong>GLM Tool Calling</strong><small>经白名单工具只读查询快照，按运行和指标校验证据</small></div>
            <div class="d4-node"><strong>人工审核</strong><small>运营人员批准或驳回，工作台将审核决定写入 SQLite</small></div>
          </div>
        </div>
    """)
    st.subheader("两种使用方式")
    for column, title, text in zip(
        st.columns(2),
        ("本地完整系统", "在线只读演示"),
        ("加载原始数据、运行求解、保存方案、人工审核、导出。可选配置智谱 GLM。",
         "切换三个预计算路线案例，查看真实 AI 验收摘录与历史评估结果。"),
        strict=True,
    ):
        with column:
            st.markdown(f"**{title}**")
            st.write(text)


def evidence_summary(result: dict) -> str:
    if "record_count" in result:
        return f"找到 {result['record_count']} 条规划。最新记录为 {result['latest_run_id']}。"
    return " ".join(
        f"{METRIC_NAMES[key]} · 历史 {metric['historical']:,.1f} 秒，"
        f"建议 {metric['suggested']:,.1f} 秒，差值 {metric['delta']:+,.1f} 秒。"
        for key, metric in result["metrics"].items()
    )


def show_ai(evidence: dict) -> None:
    st.title("AI运营助手")
    st.info("历史真实调用展示，非实时AI对话")
    cases = {case["id"]: case for case in evidence["cases"]}
    case_id = st.radio("历史调用案例", tuple(cases), format_func=lambda key: cases[key]["title"],
                       horizontal=True, key="ai_case")
    case = cases[case_id]
    st.caption(f"M6-5A 真实验收报告摘录 · {evidence['ai']['model']} · 运行 ID 使用一致代号，工具返回仅列相关字段。")
    for index, turn in enumerate(case["turns"], 1):
        with st.container(border=True):
            st.markdown(f"**第 {index} 轮 · 业务问题**")
            st.html(f'<p class="d4-turn-question">{escape(turn["question"])}</p>')
            model_tools = [tool for tool in turn["tools"] if tool["origin"] == "model"]
            fallback_tools = [tool for tool in turn["tools"] if tool["origin"] == "evidence_fallback"]
            if model_tools:
                tool_names = " → ".join(f"`{tool['name']}`" for tool in model_tools)
                st.markdown(f"**模型调用** · {tool_names}")
            else:
                st.caption("模型本轮未调用工具。")
            original, checked = st.columns(2)
            with original:
                st.markdown("**模型原始回答**")
                st.text(turn["model_answer"])
            with checked:
                st.markdown("**应用证据检查**")
                st.write(turn["application_check"])
                if fallback_tools:
                    st.markdown(f"**应用补查** · `{fallback_tools[0]['name']}`")
                st.markdown("**最终展示结果**")
                st.success(turn["displayed_answer"])
            with st.expander("工具参数与返回证据"):
                for tool in turn["tools"]:
                    owner = "模型" if tool["origin"] == "model" else "应用补查"
                    st.markdown(f"**{owner} · `{tool['name']}`**")
                    st.json(tool["arguments"])
                    st.write(evidence_summary(tool["result"]))
    st.caption("权限边界 · 工具仅查询规划快照，审核由人工在工作台完成。")


def show_evaluation(evidence: dict) -> None:
    routes, ai = evidence["route"], evidence["ai"]
    size = routes["evaluated"]
    st.title("项目评估")
    st.subheader("路线规划 · M6-4")
    st.caption(f"固定 {size} 条真实历史路线 · 单车闭环 · 20 秒搜索预算 · 离线模拟")
    with st.container(key="evaluation_metrics"):
        show_metrics((
            ("求解成功", f"{routes['solved']}/{size}"),
            ("预计行驶减少", f"{routes['driving_improved']}/{size}"),
            ("预计总作业减少", f"{routes['total_improved']}/{size}"),
        ))
        show_metrics((
            ("平均预计行驶降幅", f"{routes['driving_mean_improvement_percent']:.2f}%"),
            ("平均预计总作业降幅", f"{routes['total_mean_improvement_percent']:.2f}%"),
        ))
    st.warning(f"{routes['total_worsened']} 条路线预计总作业时间增加。{routes['new_risk_routes']} 条路线出现新增模拟窗口风险，"
               f"涉及 {routes['new_risk_packages']} 个包裹、{routes['new_risk_stops']} 个站点。")
    st.caption("均值含全部可比较路线。求解目标是预计行驶时间，服务、等待和风险在求解后模拟。"
               "固定样本未覆盖零有效窗口分层，指标只能描述本次样本的模拟表现。")
    st.subheader("AI 可靠性 · 两组独立验收")
    turns = ai["independent_turns"]
    baseline, independent = st.columns(2)
    with baseline, st.container(border=True):
        st.markdown("**M6-3B · 原始真实模型评估**")
        st.metric("通过人工复核", f"{ai['baseline_passed']}/{ai['baseline_cases']}")
        st.caption("典型失败：选错最新运行、混淆等待和总作业时间、遗漏离线模拟口径。")
    with independent, st.container(border=True):
        st.markdown("**M6-5A · 工程修复后独立验收**")
        st.caption(f"{ai['independent_scenarios']} 个新场景 · {turns} 轮对话")
        st.markdown(f"- 模型原始数值正确 **{ai['model_numeric_correct']}/{turns}** 轮\n"
                    f"- 应用补查后最终数值正确 **{ai['display_numeric_correct']}/{turns}** 轮\n"
                    f"- 最终正文明确标注离线模拟 **{ai['body_marked_offline']}/{turns}** 轮")
    st.caption("基线按综合要求逐题人工复核，独立验收逐轮核对数值与提示。两组用例与判定口径各自独立。")
    st.markdown(f"[阅读公开评估报告 ↗]({REPORT_URL})")


def show_attribution() -> None:
    st.divider()
    st.caption("数据来源：[2021 Amazon Last Mile Routing Research Challenge Dataset]"
               "(https://registry.opendata.aws/amazon-last-mile-challenges/) · Copyright Amazon.com, Inc. or its affiliates · "
               "[CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/) · 汇总和呈现方式已作修改。")
