"""Join and audit completed batches of the fixed Amazon sample."""

import argparse
import csv
import hashlib
import json
from decimal import Decimal
from math import isclose, isfinite
from os.path import relpath
from pathlib import Path
from statistics import mean, median

from evals.evaluate_routes import FIELDS, MANIFEST, METRICS, change_percent, evaluation_protocol, write_results


EXTRA_FIELDS = ("comparable", "driving_delta_seconds", "total_operation_delta_seconds")
TIME_FIELDS = ("driving", "total_operation")


def summarize_rows(rows: list[dict]) -> dict:
    comparable = [row for row in rows if row["comparable"]]
    attempted = [row for row in rows if row["solve_status"] in {"succeeded", "failed"}]
    solved = sum(row["solve_status"] == "succeeded" for row in attempted)
    result = {
        "completed_count": len(rows), "comparable_count": len(comparable), "failure_count": len(rows) - len(comparable),
        "solver_attempted_count": len(attempted), "solver_success_count": solved,
        "solver_success_rate": solved / len(attempted) if attempted else None,
    }
    for metric in TIME_FIELDS:
        delta = [row[f"{metric}_delta_seconds"] for row in comparable]
        improvements = [-row[f"{metric}_change_percent"] for row in comparable if row[f"{metric}_change_percent"] is not None]
        result[metric] = {
            "denominator": len(delta), "improved": sum(value < -1e-6 for value in delta),
            "unchanged": sum(abs(value) <= 1e-6 for value in delta), "worsened": sum(value > 1e-6 for value in delta),
            "percentage_denominator": len(improvements),
            "mean_improvement_percent": mean(improvements) if improvements else None,
            "median_improvement_percent": median(improvements) if improvements else None,
            "min_improvement_percent": min(improvements) if improvements else None,
            "max_improvement_percent": max(improvements) if improvements else None,
        }
    times = [row["solve_seconds"] for row in comparable if row["solve_seconds"] is not None]
    result["solve_seconds"] = {"denominator": len(times), "mean": mean(times) if times else None, "max": max(times) if times else None}
    for kind in ("package", "stop"):
        historical = [row[f"historical_risk_{kind}_count"] for row in comparable]
        suggested = [row[f"suggested_risk_{kind}_count"] for row in comparable]
        new = [row[f"new_risk_{kind}_count"] for row in comparable if row[f"new_risk_{kind}_count"] is not None]
        result[f"risk_{kind}"] = {
            "denominator": len(comparable),
            "historical_count": sum(historical), "suggested_count": sum(suggested),
            "historical_routes": sum(value > 0 for value in historical), "suggested_routes": sum(value > 0 for value in suggested),
            "new_denominator": len(new), "new_routes": sum(value > 0 for value in new), "new_count": sum(new),
        }
    return result


def audit_row(row: dict, sample: dict) -> dict:
    for key in ("position", "route_id", "route_score", "stop_count", "package_count", "valid_time_window_count"):
        if row[key] != sample[key]:
            raise ValueError(f"Sample {sample['position']} disagrees with the frozen manifest on {key}")
    comparable = (
        row["data_validation_status"] == "passed" and row["solve_status"] == "succeeded"
        and row["found_solution"] is True and row["tour_verified"] is True and not row["failure_reason"]
    )
    result = {**row, "comparable": comparable, "driving_delta_seconds": None, "total_operation_delta_seconds": None}
    for kind in ("package", "stop"):
        result.setdefault(f"new_risk_{kind}_count", None)
    if not comparable:
        if not row["failure_reason"]:
            raise ValueError(f"Sample {sample['position']} lacks a failure reason")
        return result
    for side in ("historical", "suggested"):
        if not all(isfinite(row[f"{side}_{key}"]) and row[f"{side}_{key}"] >= 0 for key in METRICS):
            raise ValueError(f"Sample {sample['position']} has invalid {side} metrics")
        total = sum(row[f"{side}_{key}"] for key in ("driving_seconds", "waiting_seconds", "planned_service_time_seconds_sum"))
        if not isclose(total, row[f"{side}_total_operation_seconds"], rel_tol=1e-12, abs_tol=1e-6):
            raise ValueError(f"Sample {sample['position']} has inconsistent {side} total operation time")
    for metric in TIME_FIELDS:
        historical = row[f"historical_{metric}_seconds"]
        suggested = row[f"suggested_{metric}_seconds"]
        result[f"{metric}_delta_seconds"] = float(Decimal(str(suggested)) - Decimal(str(historical)))
        computed = change_percent(historical, suggested)
        saved = row[f"{metric}_change_percent"]
        if (computed is None and saved is not None) or (computed is not None and (saved is None or not isclose(computed, saved))):
            raise ValueError(f"Sample {sample['position']} has inconsistent {metric} change")
    for kind in ("package", "stop"):
        historical = row[f"historical_risk_{kind}_count"]
        suggested = row[f"suggested_risk_{kind}_count"]
        key = f"new_risk_{kind}_count"
        if result[key] is None and (historical == 0 or suggested == 0):
            # Legacy pilot rows store totals only. A zero on either side makes the set difference known.
            result[key] = suggested
        if result[key] is not None and not max(suggested - historical, 0) <= result[key] <= suggested:
            raise ValueError(f"Sample {sample['position']} has inconsistent newly risky {kind} count")
    return result


def combine_batches(batch_dirs: list[Path], manifest_path: Path) -> dict:
    manifest_bytes = manifest_path.read_bytes()
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    manifest = json.loads(manifest_bytes)
    samples = {row["position"]: row for row in manifest["samples"]}
    rows = {}
    source_batches = []
    for directory in batch_dirs:
        source = directory / "results.json"
        source_bytes = source.read_bytes()
        report = json.loads(source_bytes)
        if report["manifest_sha256"] != manifest_hash or report["protocol"] != evaluation_protocol():
            raise ValueError(f"Manifest or protocol mismatch in {source}")
        with (directory / "results.csv").open(encoding="utf-8", newline="") as file:
            csv_rows = list(csv.DictReader(file))
        positions = [row["position"] for row in report["results"]]
        if positions != report["sample_positions"] or positions != report.get("requested_positions", positions):
            raise ValueError(f"Incomplete or out-of-order batch in {source}")
        for row, csv_row in zip(report["results"], csv_rows, strict=True):
            if csv_row != {key: "" if value is None else str(value) for key, value in row.items()}:
                raise ValueError(f"CSV differs from JSON in {source} at position {row['position']}")
            position = row["position"]
            if position in rows or position not in samples:
                raise ValueError(f"Repeated or unknown sample position {position}")
            rows[position] = audit_row(row, samples[position])
        source_batches.append({
            "path": str(source.resolve()), "sha256": hashlib.sha256(source_bytes).hexdigest(),
            "sample_positions": positions, "started_at_utc": report["started_at_utc"],
            "elapsed_seconds": report["elapsed_seconds"], "staging": report["staging"],
        })
    if rows.keys() != samples.keys():
        raise ValueError(f"Missing frozen sample positions: {sorted(samples.keys() - rows.keys())}")
    results = [rows[position] for position in sorted(rows)]
    return {
        "manifest_sha256": manifest_hash, "manifest_version": manifest["version"], "seed": manifest["seed"],
        "frozen_sample_count": len(samples), "empty_strata": [cell for cell in manifest["strata"] if not cell["candidate_count"]],
        "protocol": evaluation_protocol(), "source_batches": source_batches,
        "statistics": summarize_rows(results), "results": results,
    }


def display(value, digits: int = 1) -> str:
    return "—" if value is None else f"{value:,.{digits}f}"


def markdown(report: dict, output: Path) -> str:
    stats = report["statistics"]
    n = stats["completed_count"]
    paired = stats["comparable_count"]
    lines = [
        "# M6-4B 固定样本路线评估", "",
        f"冻结样本共 {report['frozen_sample_count']} 条，已有结果 {n} 条，可比较 {paired} 条。"
        f"求解成功 {stats['solver_success_count']}/{stats['solver_attempted_count']} 条实际发起求解的路线。"
        f"批量评估失败 {stats['failure_count']} 条。", "",
        "两组结果使用同一个冻结清单和评估协议。第 1～3 条来自试运行，第 4～12 条来自续跑批次。",
        "协议为单车闭环、每个投递点访问一次、相同有向交通矩阵与 UTC 出发时刻。"
        "OR-Tools 使用 PATH_CHEAPEST_ARC、GUIDED_LOCAL_SEARCH 和每条 20 秒搜索预算。", "",
        "## 逐路线结果", "",
        "| 样本 | Route ID | 站点 / 包裹 / 有效窗 | 数据校验 | 求解状态 | 求解秒数 | 失败阶段与原因 |",
        "|---:|---|---:|---|---|---:|---|",
    ]
    for row in report["results"]:
        failure = " · ".join(filter(None, (row["failure_stage"], row["failure_reason"]))) or "—"
        failure = failure.replace("|", r"\|").replace("\n", " ")
        size = " / ".join(str(row[key]) for key in ("stop_count", "package_count", "valid_time_window_count"))
        status = row["solver_status"] or row["solve_status"]
        lines.append(f"| {row['position']} | {row['route_id']} | {size} | {row['data_validation_status']} | {status} | {display(row['solve_seconds'], 3)} | {failure} |")
    lines += [
        "", "时间单位为秒。箭头表示历史到建议，差值与变化率按建议减历史计算。", "",
        "| 样本 | 行驶 | 行驶差值 / 变化率 | 总作业 | 总作业差值 / 变化率 | 等待 | 风险包裹 | 风险站点 | 新增风险包裹 / 站点 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["results"]:
        paired_values = [" → ".join(display(row[f"{side}_{field}"], digits) for side in ("historical", "suggested"))
                         for field, digits in (("driving_seconds", 1), ("total_operation_seconds", 1), ("waiting_seconds", 1),
                                               ("risk_package_count", 0), ("risk_stop_count", 0))]
        driving, total, waiting, packages, stops = paired_values
        changes = [
            f"{display(row[f'{metric}_delta_seconds'])} / {display(row[f'{metric}_change_percent'], 2)}%"
            if row[f"{metric}_change_percent"] is not None else f"{display(row[f'{metric}_delta_seconds'])} / —"
            for metric in TIME_FIELDS
        ]
        new = " / ".join(display(row[f"new_risk_{kind}_count"], 0) for kind in ("package", "stop"))
        lines.append(f"| {row['position']} | {driving} | {changes[0]} | {total} | {changes[1]} | {waiting} | {packages} | {stops} | {new} |")
    lines += [
        "", "## 汇总统计", "",
        "改善百分比为（历史 − 建议）÷ 历史，正值表示减少。均值、中位数和范围包含可比较样本中可计算的全部百分比。"
        "历史值为零时百分比不适用。绝对时间差在百万分之一秒内视为持平。", "",
        "| 指标 | 改善 / 持平 / 恶化 | 比较分母 | 百分比分母 | 平均改善 | 中位改善 | 改善范围 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, key in (("行驶时间", "driving"), ("总作业时间", "total_operation")):
        metric = stats[key]
        counts = " / ".join(str(metric[k]) for k in ("improved", "unchanged", "worsened"))
        values = [display(metric[f"{k}_improvement_percent"], 2) for k in ("mean", "median", "min", "max")]
        lines.append(f"| {name} | {counts} | {metric['denominator']} | {metric['percentage_denominator']} | {values[0]}% | {values[1]}% | {values[2]}% 至 {values[3]}% |")
    for label, key in (("包裹", "risk_package"), ("站点", "risk_stop")):
        risk = stats[key]
        lines += [
            "",
            f"窗口风险{label}在历史方案中有 {risk['historical_count']} 个，涉及 {risk['historical_routes']}/{risk['denominator']} 条路线。"
            f"建议方案有 {risk['suggested_count']} 个，涉及 {risk['suggested_routes']}/{risk['denominator']} 条路线。"
            f"建议方案新增 {risk['new_count']} 个，涉及 {risk['new_routes']}/{risk['new_denominator']} 条可确定新增数量的路线。",
        ]
    timing = stats["solve_seconds"]
    lines += [
        "",
        "新增风险包裹按两种方案的（站点、包裹）集合差计算，新增风险站点按风险站点集合差计算。"
        "新增与风险总数的净变化是两个不同指标。试运行的历史风险均为零，其新增数量可直接从建议侧总数确定。", "",
        f"平均求解耗时 {display(timing['mean'], 3)} 秒，最大 {display(timing['max'], 3)} 秒，分母为 {timing['denominator']} 条可比较路线。"
        "求解成功率以实际发起求解的路线为分母。两方案比较按通过数据、闭环与时间一致性校验的样本计算。", "",
        "## 局限与工程演示", "",
        "这是固定 12 条样本的探索性评估。三个站点规模档的零有效时间窗层均无候选，已按冻结规则从邻层补样。"
        "结果只描述这些样本。当前求解目标为最小化行驶时间，服务和时间窗口在后验模拟中评价。"
        "模拟时间与风险用于比较方案，不能推断实际运营收益、全局最优或整个 Amazon 配送总体的表现。", "",
    ]
    if paired == report["frozen_sample_count"]:
        lines.append("工程演示验收通过。冻结选样、批次续跑、闭环校验、双方案模拟和可追溯报告已贯通，可以展示预计行驶时间和业务风险之间的取舍。")
    else:
        lines.append(f"工程演示中应展示 {stats['failure_count']} 条失败记录及原因，成功路线可用于展示完整的比较流程。")
    lines += ["", "原始批次报告"]
    for source in report["source_batches"]:
        positions = source["sample_positions"]
        relative = Path(relpath(source["path"], output)).as_posix()
        lines.append(f"- [样本 {positions[0]}～{positions[-1]}]({relative})，批次用时 {display(source['elapsed_seconds'], 2)} 秒。")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batches", nargs="+", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    report = combine_batches(args.batches, MANIFEST)
    write_results(args.output_dir, report, FIELDS + EXTRA_FIELDS)
    with (args.output_dir / "summary.md").open("x", encoding="utf-8") as file:
        file.write(markdown(report, args.output_dir.resolve()))
    print(args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
