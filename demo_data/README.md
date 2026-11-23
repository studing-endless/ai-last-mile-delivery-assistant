# D1 三条精选演示案例

本目录提供真实计算结果的只读离线快照。三条案例从既有证据中精选，用于解释效率改善、等待取舍及时间窗口风险。原有固定 12 条路线评估仍是整体表现的统计依据。

## 读取与验证

在项目根目录运行，读取和独立验证只使用 Python 标准库。

```powershell
python -m evals.validate_demo_data
python -c "from evals.validate_demo_data import load_package; print([(c['demo_case_id'], c['route_id']) for c in load_package()])"
```

维护者持有既有本地证据时，可以执行来源比对或重新生成。生成程序读取已保存的汇总、原始试运行报告、冻结选样清单、元数据索引，以及通过现有只读接口取得的规划快照。

```powershell
.\.venv\Scripts\python.exe -B -m evals.build_demo_data --check-source
.\.venv\Scripts\python.exe -B -m evals.build_demo_data
```

缺少证据时，来源比对和生成会报错。公开包的结构、时间合计与差值验证可独立执行。仅有公开包时，源文件指纹用于核对未来取得的原件，无法独立证明原件内容。生成结果不带运行时刻，因此相同证据生成相同字节。

## 案例与出处

| 案例 | 原始 Amazon Route ID | 证据 |
| --- | --- | --- |
| A 效率改善 | `RouteID_61612c45-4ab6-4ad7-af11-a9ad4ded4765` | M6-4 冻结样本第 1 条 |
| B 效率权衡 | `RouteID_00143bdd-0a6b-49ec-bb35-36593d303e77` | 保存运行 `1040cb65-98e0-4eac-8212-9c9dab17c848` |
| C 窗口风险 | `RouteID_fbc6e4e2-5cb2-4367-a24d-833b76698f0f` | M6-4 冻结样本第 3 条 |

A、C 的时长与风险计数逐字段比对原始试运行和 M6-4 汇总，站点及包裹计数核对冻结清单。B 的时长与审核状态来自保存快照，任务计数来自既有 6,112 路线元数据索引，站点数另与保存的闭环顺序核对。每个案例的 `provenance` 记录仓库相对出处、定位条件和 SHA-256。私有出处留在本地，公开复现清单位于 `evals/route_sample_manifest.json`。

## 字段与语义

- `demo_case_id` 为演示编号。`route_id` 为原始公开数据标识。`run_id` 仅 B 有真实保存值，A、C 为 `null`。
- `task` 包含站点数（含配送站）、包裹记录数和完整有效时间窗口数。
- `historical`、`suggested` 保留原始秒数和风险计数。服务字段表示各包裹计划服务时间简单合计。总作业包含闭合行程的行驶、计划服务与等待。窗口风险使用服务开始时间代理逐包裹检查，端点包含在内。
- `comparisons` 的差值和百分比按建议减历史计算。时间减少为改善，风险计数减少为风险降低。零历史基数的百分比为 `null`。单位分别为秒、包裹或站点。
- `new_risk` 与净变化分别存储。B 未保存新增风险集合字段，明确为 `null`，其两侧风险总数均保留。A、C 保留已核验的新增计数。
- `review_status` 仅保留已有审核决定，A、C 为 `null`。`missing_data` 解释缺失字段。数据包不含私人审核备注、包裹标识、坐标、访问顺序或交通矩阵。
- `algorithm` 保留算法名、20 秒预算和已保存参数。`simulation` 固定离线估计口径。推荐顺序对应的指标已经预先计算。

精度规则采用现有路线汇总校验的绝对容差 0.000001 秒。JSON 拒绝重复字段、NaN 和 Infinity。案例文件指纹、指标非负性、服务合计、差值、百分比、风险边界与案例分类分别检查。

## 许可与公开字段审查

数据源为 [2021 Amazon Last Mile Routing Research Challenge Dataset](https://registry.opendata.aws/amazon-last-mile-challenges/)。Copyright Amazon.com, Inc. or its affiliates. 引用 Merchán, D., Arora, J., Pachon, J., Konduri, K., Winkenbach, M., Parks, S., & Noszek, J. (2022), *Transportation Science*, [数据集论文](https://www.amazon.science/publications/2021-amazon-last-mile-routing-research-challenge-data-set)。官方条款核查于 2026-11-23。

Amazon 数据及本目录派生展示按 [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/) 标注来源、变更和非商业使用限制。变更为从既有模拟结果摘取汇总字段，并增加演示说明与可核查差值。许可证要求合理署名、附许可链接、说明修改及遵守非商业限制。项目代码的 MIT 许可与数据许可分别适用。

拟公开内容限于三条公开 Route ID、汇总指标、求解参数、证据指纹和 B 的软件运行 ID及审核状态。B 的运行 ID 按本阶段明确要求公开，用于与维护者原件关联。原始 JSON、SQLite、密钥、审核备注和敏感日志留在本地。求职公开展示的具体使用方式仍需遵守原数据许可，商业使用须另行确认授权。

后续接入页面时，可调用 `load_package()` 提供案例切换、指标表和风险提示。D1 只验收数据包与验证程序。
