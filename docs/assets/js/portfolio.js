const select = (selector) => document.querySelector(selector);
const all = (selector) => document.querySelectorAll(selector);
const caseContent = select("#casecontent");
const trace = select("#trace");

const metrics = [
  ["driving_seconds", "行驶时间（秒）"],
  ["planned_service_time_seconds_sum", "计划服务合计（秒）"],
  ["waiting_seconds", "等待时间（秒）"],
  ["total_operation_seconds", "总作业时间（秒）"],
  ["risk_package_count", "风险包裹（个）"],
  ["risk_stop_count", "风险站点（个）"],
];
const colors = ["#80a88b", "#355d49", "#e8a061"];
const caseNames = { A: "效率改善", B: "效率权衡", C: "时间窗口风险" };
const aiScenarios = {
  fix: { id: "A02", title: "多轮追问：等待时间增加了多少？" },
  latest: { id: "A01", title: "查询最近一次保存的规划与建议行驶时间" },
  approval: { id: "A03", title: "查询计划服务时间，并要求代为批准" },
};
const escape = (value) => String(value).replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));
const format = (value, digits = 1) => value == null ? "未提供" : new Intl.NumberFormat("en-US", {
  minimumFractionDigits: digits, maximumFractionDigits: digits,
}).format(value);
const signed = (value, digits = 1) => value == null ? "未提供" :
  `${value > 0 ? "+" : value < 0 ? "−" : ""}${format(Math.abs(value), digits)}`;
const seconds = (value) => value == null ? "未提供" : `${format(value)} 秒`;
const fixedCount = (value) => format(value, 0);
const changedSeconds = (value) => value == null ? "未提供" : `${signed(value)} 秒`;
const traceHeading = (model) => `真实验收报告摘要 · ${escape(model)} · 非实时 AI · 无 API 调用
  <span class="screen-reader">。运行 ID 使用代号，工具返回为相关字段摘录。</span>`;

function activate(buttons, key, dataKey) {
  for (const button of buttons) {
    const active = button.dataset[dataKey] === key;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  }
}

function drawSummary(payload, caseB) {
  const { route, ai } = payload.evaluation;
  const ratio = (count, total, separator = "/") => `${count}${separator}${total}`;
  const counts = {
    driving_improved: route.driving_improved,
    total_improved: route.total_improved,
    new_risk_routes: route.new_risk_routes,
  };
  const stats = {
    ...route,
    ...ai,
    indexed: fixedCount(route.indexed),
    delivery_count: String(payload.delivery_cases.length).padStart(2, "0"),
    solved: ratio(route.solved, route.evaluated),
    driving_mean: `${format(route.driving_mean_improvement_percent, 2)}%`,
    total_mean: `${format(route.total_mean_improvement_percent, 2)}%`,
    baseline_passed: ratio(ai.baseline_passed, ai.baseline_cases),
  };
  for (const [key, value] of Object.entries(counts)) stats[key] = ratio(value, route.evaluated, " / ");
  for (const key of ["model_numeric_correct", "display_numeric_correct", "body_marked_offline"]) {
    stats[key] = ratio(ai[key], ai.independent_turns);
  }
  for (const element of all("[data-stat]")) element.textContent = stats[element.dataset.stat];
  for (const element of all("[data-progress]")) {
    element.style.width = `${100 * counts[element.dataset.progress] / route.evaluated}%`;
  }
  select("#hero-case-size").textContent = `${fixedCount(caseB.task.stop_count)} 站点 · ${fixedCount(caseB.task.package_count)} 包裹`;
  const drivingDelta = caseB.comparisons.driving_seconds.delta;
  select("#hero-driving").textContent = signed(drivingDelta, Number.isInteger(drivingDelta) ? 0 : 1);
  select("#hero-operation").textContent = signed(caseB.comparisons.total_operation_seconds.delta);
}

function caseMessage(delivery) {
  const { demo_case_id: id, comparisons, new_risk: risk, review_status: review } = delivery;
  if (id === "A") {
    return {
      verdict: "可考虑人工复核",
      risk: `新增模拟风险：${fixedCount(risk.package_count)} 个包裹、${fixedCount(risk.stop_count)} 个站点。`,
      note: "行驶时间减少，等待没有增加，因此总作业时间同步下降。模拟风险未增加，方案仍须人工审核。",
    };
  }
  if (id === "B") {
    const historical = delivery.historical;
    const suggested = delivery.suggested;
    const equalRisk = historical.risk_package_count === suggested.risk_package_count;
    const count = equalRisk ? `两侧风险包裹数均为 ${fixedCount(historical.risk_package_count)}` :
      `风险包裹：历史 ${fixedCount(historical.risk_package_count)}，建议 ${fixedCount(suggested.risk_package_count)}`;
    const drivingDelta = comparisons.driving_seconds.delta;
    return {
      verdict: "总作业时间增加",
      risk: `${count}；新增风险集合${risk.package_count == null ? "未提供。" : `涉及 ${fixedCount(risk.package_count)} 个包裹。`}`,
      note: `行驶少 ${seconds(drivingDelta == null ? null : Math.abs(drivingDelta))}，却多等待 ${seconds(comparisons.waiting_seconds.delta)}，最终总作业增加 ${seconds(comparisons.total_operation_seconds.delta)}。` +
        (review === "REJECTED" ? "该历史快照已被人工驳回（REJECTED）。" : ""),
    };
  }
  return {
    verdict: "新增时间窗口风险",
    risk: `新增 ${fixedCount(risk.package_count)} 个模拟风险包裹，涉及 ${fixedCount(risk.stop_count)} 个站点。`,
    note: "预计总作业时间下降，但出现新的窗口风险，需要人工复核受影响的配送承诺。",
  };
}

function renderBars(side, label, max) {
  const total = side.total_operation_seconds;
  const fields = metrics.slice(0, 3);
  const complete = total != null && fields.every(([key]) => side[key] != null);
  const bars = complete ? fields.map(([key, field], index) => {
    const width = max > 0 ? 100 * side[key] / max : 0;
    return `<div data-width="${width}" style="width:${width}%;background:${colors[index]}" title="${field}：${format(side[key])}"></div>`;
  }).join("") : "";
  return `<div class="bar-row">
    <div class="bar-label"><span>${label}</span><b>${total == null ? "未提供" : `${format(total / 60, 2)} 分钟`}</b></div>
    ${complete ? `<div class="bar" role="img" aria-label="${label}：${fields.map(([key, field]) => `${field} ${format(side[key])}`).join("，")}">${bars}</div>` :
      '<p class="caption">部分时长未提供。</p>'}
  </div>`;
}

function selectCase(delivery) {
  const id = delivery.demo_case_id;
  const { task, historical, suggested, comparisons } = delivery;
  const total = comparisons.total_operation_seconds;
  const max = Math.max(historical.total_operation_seconds ?? 0, suggested.total_operation_seconds ?? 0);
  const warn = id === "A" ? "" : "warn";
  const message = caseMessage(delivery);
  const taskSummary = `${fixedCount(task.stop_count)} 站点 · ${fixedCount(task.package_count)} 包裹` +
    (id === "B" ? "" : ` · ${fixedCount(task.valid_time_window_count)} 有效时间窗口`);
  const oldBars = Array.from(caseContent.querySelectorAll("[data-width]"), (bar) => bar.dataset.width);
  activate(all(".case"), id, "case");
  caseContent.innerHTML = `
    <div class="case-header"><div><h3>${id} / ${caseNames[id]}</h3><p class="case-meta">${taskSummary}</p></div>
      <span class="verdict ${warn}">${message.verdict}</span></div>
    <div class="case-main"><div class="chart"><div class="minihead"><span>作业时间构成</span><span>分钟</span></div>
      ${renderBars(historical, "历史路线", max)}${renderBars(suggested, "建议路线", max)}
      <div class="legend light">${["行驶", "计划服务", "等待"].map((label, index) => `<span style="--c:${colors[index]}">${label}</span>`).join("")}</div></div>
      <div class="insight"><div class="caption">预计总作业时间变化</div>
        <div class="big">${total.change_percent == null ? "未提供" : `${signed(total.change_percent, 2)}%`}</div>
        <div class="delta ${total.delta > 0 ? "warn" : ""}">${changedSeconds(total.delta)}</div>
        <div class="risk">${message.risk}</div></div></div>
    <div class="decision ${warn}">${message.note}</div>
    <div class="tablewrap" tabindex="0" aria-label="路线指标对比，可横向滚动"><table>
      <thead><tr><th>指标</th><th>历史</th><th>建议</th><th>差值</th></tr></thead>
      <tbody>${metrics.map(([key, label], index) => {
        const digits = index > 3 ? 0 : 1;
        return `<tr><td>${label}</td><td>${format(historical[key], digits)}</td><td>${format(suggested[key], digits)}</td><td>${signed(comparisons[key].delta, digits)}</td></tr>`;
      }).join("")}</tbody></table></div>`;
  const bars = Array.from(caseContent.querySelectorAll("[data-width]"));
  if (oldBars.length === bars.length && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    for (const [index, bar] of bars.entries()) bar.style.width = `${oldBars[index]}%`;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      for (const bar of bars) bar.style.width = `${bar.dataset.width}%`;
    }));
  }
}

function returnedMetric(turn, key) {
  return turn.tools.find((tool) => tool.result.metrics?.[key]).result.metrics[key];
}

function aiSteps(key, history) {
  const first = history.turns[0];
  if (key === "fix") {
    const followup = history.turns[1];
    const waiting = returnedMetric(followup, "waiting_seconds");
    const wrong = followup.model_answer.match(/[\d.,]+\s*秒/)?.[0];
    return [
      ["模型原答", `<span class="fail">${wrong ? `增加 ${escape(wrong)}` : escape(followup.model_answer)}（错误）</span>`],
      ["应用证据补查", `核查同一规划运行：历史等待 ${Number.isInteger(waiting.historical) ? fixedCount(waiting.historical) + " 秒" : seconds(waiting.historical)}，建议等待 ${seconds(waiting.suggested)}。`],
      ["最终展示", `<span class="answer">增加 ${seconds(waiting.delta)}</span>`],
    ];
  }
  if (key === "latest") {
    const driving = returnedMetric(first, "driving_seconds");
    return [
      ["模型自主选择工具", `<code>${first.tools.map((tool) => escape(tool.name)).join(" → ")}</code>`],
      ["应用证据检查", escape(first.application_check)],
      ["最终展示", `<span class="answer">预计行驶 ${format(driving.suggested, Number.isInteger(driving.suggested) ? 0 : 1)} 秒</span>`],
    ];
  }
  const service = returnedMetric(first, "planned_service_time_seconds_sum");
  const review = first.application_check.split("。").find((text) => text.includes("PENDING"));
  return [
    ["模型原答", `查到计划服务合计 ${seconds(service.suggested)}，但未回应代批要求。`],
    ["应用权限边界", escape(first.displayed_answer.split("\n")[0]) + " 保留有证据的业务数值。"],
    ["最终展示", `<span class="answer">${seconds(service.suggested)}</span><br/>${escape(review ?? "审核状态未提供")}。`],
  ];
}

function toolEvidence(tool) {
  const result = tool.result;
  const returned = result.metrics ? Object.entries(result.metrics).map(([key, value]) => {
    const subject = metrics.find(([metric]) => metric === key)?.[1] ?? key;
    return `${escape(subject)}：历史 ${seconds(value.historical)}，建议 ${seconds(value.suggested)}，差值 ${changedSeconds(value.delta)}。`;
  }).join(" ") : `找到 ${fixedCount(result.record_count)} 条规划；最新运行 ${escape(result.latest_run_id)}。`;
  const origin = tool.origin === "evidence_fallback" ? "应用补查" : "模型调用";
  return `<li><strong>${origin}</strong> <code>${escape(tool.name)}</code>
    <div>参数 <code>${escape(JSON.stringify(tool.arguments))}</code></div><div>${returned}</div></li>`;
}

function traceDetails(history) {
  return `<details class="trace-details"><summary>查看原始问题、回答与工具证据</summary>${history.turns.map((turn, index) => `
    <div class="trace-turn"><h4>第 ${index + 1} 轮</h4>
      <div><b>业务问题</b> ${escape(turn.question)}</div>
      <div><b>模型原答</b> ${escape(turn.model_answer)}</div>
      <div><b>检查说明</b> ${escape(turn.application_check)}</div>
      <div><b>最终展示</b> ${escape(turn.displayed_answer).replaceAll("\n", "<br/>")}</div>
      <ul>${turn.tools.map(toolEvidence).join("")}</ul>
    </div>`).join("")}<div>运行 ID 使用一致代号。工具返回只摘录相关字段。</div></details>`;
}

function selectAi(key, history, model) {
  activate(all(".ai-choice"), key, "ai");
  trace.innerHTML = `<div class="eyebrow">Historical evaluation / GLM</div><h3>${aiScenarios[key].title}</h3>
    ${aiSteps(key, history).map(([title, content], index) => `
      <div class="step"><span class="node">0${index + 1}</span><div><b>${title}</b><div>${content}</div></div></div>`).join("")}
    <div class="trace-note">${traceHeading(model)}</div>${traceDetails(history)}`;
}

async function loadPortfolio() {
  const inlineData = select("#portfolio-data");
  if (inlineData) return JSON.parse(inlineData.textContent);
  const response = await fetch(new URL("../data/portfolio.json", import.meta.url));
  if (!response.ok) throw new Error("Public portfolio snapshot could not be loaded.");
  return response.json();
}

async function start() {
  const payload = await loadPortfolio();
  if (payload.schema_version !== 1) throw new Error("Unsupported public portfolio snapshot version.");
  const cases = new Map(payload.delivery_cases.map((item) => [item.demo_case_id, item]));
  const history = new Map(payload.ai_history.map((item) => [item.id, item]));
  const showAi = (key) => selectAi(key, history.get(aiScenarios[key].id), payload.evaluation.ai.model);
  drawSummary(payload, cases.get("B"));
  selectCase(cases.get("B"));
  showAi("fix");
  for (const button of all(".case")) button.addEventListener("click", () => selectCase(cases.get(button.dataset.case)));
  for (const button of all(".ai-choice")) button.addEventListener("click", () => showAi(button.dataset.ai));
}

start().catch(() => {
  caseContent.textContent = "公开案例数据无法加载。请通过 HTTP 服务器打开页面，并确认 portfolio.json 已同步。";
  trace.textContent = "历史验收资料暂不可用。";
});
