# 末端配送工作台演示

## 在线作品集

以下四张截图来自已上线的 V3 产品官网。Streamlit 在线工作台也嵌入同一版只读作品集。[产品官网](https://studing-endless.github.io/ai-last-mile-delivery-assistant/) · [Streamlit 在线工作台](https://ai-last-mile-assistant.streamlit.app/) · [本机快速启动](../README.md#快速启动只读演示)

### 首页

![V3 官网首页的品牌、配送网络示意与路线评估成果](screenshots/portfolio-overview.jpg)

首页展示 6,112 条历史路线的索引规模和 12 条固定样本的评估成果。配送网络为单车访问顺序示意。

### 配送方案分析

![V3 配送方案分析，B 案例的时间构成、指标对比和总作业时间告警](screenshots/portfolio-analysis.jpg)

B 案例预计行驶少 1,123.0 秒，等待多 1,347.7 秒，总作业时间增加 224.7 秒。[体验三个案例](https://studing-endless.github.io/ai-last-mile-delivery-assistant/#studio)

### AI 证据链

![V3 AI 证据链，展开两轮历史问答以及应用补查](screenshots/portfolio-ai.jpg)

AI 页面的问题、模型原答和最终文本选自 M6-5A 已保存的真实验收报告。运行 ID 用一致代号替换，工具返回只摘录相关字段。人工审核状态依据验收复核，摘录可对照[公开评估报告](../evals/results.md)与[公开证据快照](../evals/portfolio_evidence.json)。M6-3B 基线保留在评估页，M6-5A 摘录只对应独立验收。证据快照记录原始报告指纹，完整私有轨迹由维护者留存。

### 项目评估

![V3 项目评估，左侧为固定样本路线结果，右侧为 AI 基线和独立验收](screenshots/portfolio-evaluation.jpg)

路线结果同时展示行驶时间改善、总作业时间变化和新增窗口风险。AI 基线与独立验收分别采用各自的样本和评分口径。[查看项目评估](https://studing-endless.github.io/ai-last-mile-delivery-assistant/#results)

## 本地完整系统

在本机按 [README](../README.md) 准备数据并启动 Streamlit。演示目标是解释一条路线为什么值得提交人工复核，以及为什么行驶时间下降仍可能不宜采用。工作台使用公开历史记录做离线模拟。

1. **加载真实任务。** 在「当前规划」保留默认 Amazon Route ID，点击「加载配送任务」。核对配送站、配送日期、119 个站点及 276 条包裹记录。
2. **生成并保存规划。** 点击「生成建议路线」。求解使用 20 秒搜索预算，应用自动保存生成的规划快照。打开路线示意和建议访问顺序，说明单车从同一站点出发并返回。
3. **比较历史与建议。** 展开「查看全部指标」，检查总作业时间、等待和窗口风险。以「建议减历史」解释差值。若演示环境已经保有图示快照，可从「历史规划记录」打开它；新机器以本机求解出的数字为准。
4. **审核与留痕。** 在本次演示新生成的 `PENDING` 记录上，以模拟指标为依据批准或驳回。驳回需填写原因。再次打开该运行，确认审核决定、时间及备注，并核对求解状态与审核状态分别显示。
5. **让 AI 查询快照。** 如已配置智谱，选择本次 `run_id`，在「AI运营助手」询问「这次规划的行驶、等待和总作业时间分别变化了多少？」展开工具调用记录，对照页面指标和查询对象。无密钥时可展示只读提示，并使用 [公开评估报告](../evals/results.md) 讲解真实历史案例。
6. **导出可复核结果。** 回到「历史规划记录」，导出 JSON 规划报告和 CSV 站点顺序。用两份文件的 `run_id` 核对它们来自同一规划快照。JSON 包含模拟指标及审核，CSV 包含访问序号与快照中已有的时程。

## 本地完整系统截图

以下两张图来自本地 Streamlit 完整模式，已省略本地运行标识和审核备注。

![真实规划对比与总作业时间告警](screenshots/route-comparison.jpg)

既有快照的建议方案预计行驶少 1,123.0 秒，等待多 1,347.7 秒，总作业多 224.7 秒。窗口风险包裹数为 0。由数据工具和时程评估生成指标，页面保留运营判断所需的取舍。

![AI 运营助手的只读与离线模拟提示](screenshots/ai-assistant.jpg)

此图展示打开本地 AI 区域时固定出现的提示。截图采集时未提交问题。

方案截图使用 [2021 Amazon Last Mile Routing Research Challenge Dataset](https://registry.opendata.aws/amazon-last-mile-challenges/) 的真实历史路线。Copyright Amazon.com, Inc. or its affiliates. 以 [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/) 提供，截图中的指标是本项目根据该数据生成的离线模拟估计。
