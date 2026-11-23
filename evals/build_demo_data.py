"""Project saved evidence into the D1 public package without running a solver."""

import argparse
import csv
from hashlib import sha256
import json
from pathlib import Path

from src.planning_db import get_review, get_run
from evals.validate_demo_data import METRICS, TIME_FIELDS, comparison, load_package, simulation_protocol, validate_case


ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "1040cb65-98e0-4eac-8212-9c9dab17c848"


def encode(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")


def source_file(root, relative):
    return {"path": relative, "sha256": sha256((root / relative).read_bytes()).hexdigest()}


def build_cases(root=ROOT):
    summary_path = "outputs/route_eval/m6-4b-summary/results.json"
    manifest_path = "evals/route_sample_manifest.json"
    report = json.loads((root / summary_path).read_bytes())
    frozen = json.loads((root / manifest_path).read_bytes())
    if report["manifest_sha256"] != source_file(root, manifest_path)["sha256"]:
        raise ValueError("Saved summary does not match frozen sample manifest")
    rows = {row["position"]: row for row in report["results"]}
    samples = {sample["position"]: sample for sample in frozen["samples"]}
    batch = report["source_batches"][0]
    # Resolve the recorded filename inside the known local batch directory.
    batch_path = "outputs/route_eval/run-20261110T103154034878Z/results.json"
    if source_file(root, batch_path)["sha256"] != batch["sha256"]:
        raise ValueError("Pilot batch fingerprint changed")
    original = {row["position"]: row for row in json.loads((root / batch_path).read_bytes())["results"]}
    cases = []
    for case_id, position, description in (("A", 1, "效率改善，行驶与总作业均减少且无新增窗口风险"),
                                           ("C", 3, "总作业减少，但出现新增模拟窗口风险")):
        row = rows[position]
        for field in ("route_id", "stop_count", "package_count", "valid_time_window_count"):
            if row[field] != samples[position][field]:
                raise ValueError("Summary task metadata differs from frozen manifest")
        for side in ("historical", "suggested"):
            if any(row[f"{side}_{field}"] != original[position][f"{side}_{field}"] for field in METRICS):
                raise ValueError("Summary metrics differ from original pilot batch")
        if not row["tour_verified"] or not row["found_solution"] or row["failure_reason"]:
            raise ValueError("Candidate lacks validated successful evidence")
        case = {
            "demo_case_id": case_id, "description": description, "route_id": row["route_id"],
            "source_type": "m6_4_evaluation", "run_id": None,
            "task": {field: row[field] for field in ("stop_count", "package_count", "valid_time_window_count")},
            "historical": {field: row[f"historical_{field}"] for field in METRICS},
            "suggested": {field: row[f"suggested_{field}"] for field in METRICS},
            "new_risk": {"package_count": row["new_risk_package_count"], "stop_count": row["new_risk_stop_count"]},
            "review_status": None,
            "algorithm": {
                "name": report["protocol"]["solver"], "search_budget_seconds": report["protocol"]["search_budget_seconds"],
                "parameters": {"vehicles": report["protocol"]["vehicle_count"], "objective": report["protocol"]["objective"],
                               "first_solution_strategy": report["protocol"]["first_solution_strategy"],
                               "local_search_metaheuristic": report["protocol"]["local_search_metaheuristic"], "cost_scale": 1000},
                "solver_status": row["solver_status"],
            },
            "provenance": {"files": [source_file(root, p) for p in (summary_path, batch_path, manifest_path)],
                           "locator": f"results[position={position}]", "evidence_sha256": sha256(encode(row)).hexdigest()},
            "missing_data": ["run_id: batch evaluation has no planning run ID", "review_status: not saved in batch report"],
        }
        cases.append(case)

    db_path = "outputs/planning_runs.sqlite3"
    run = get_run(root / db_path, RUN_ID, read_only=True)
    review = get_review(root / db_path, RUN_ID, read_only=True)
    if run is None or review is None:
        raise ValueError("Case B saved planning snapshot or review is missing")
    index_path = "outputs/route_eval/route_metadata_index.csv"
    with (root / index_path).open(encoding="utf-8", newline="") as stream:
        metadata = [row for row in csv.DictReader(stream) if row["route_id"] == run["route_id"]]
    if len(metadata) != 1 or metadata[0]["metadata_status"] != "indexed":
        raise ValueError("Case B task metadata unavailable or ambiguous")
    # Reuse the original closed tour to cross-check the indexed stop count.
    if len(set(run["suggested_order"])) != int(metadata[0]["stop_count"]):
        raise ValueError("Case B saved order disagrees with indexed stop count")
    b = {
        "demo_case_id": "B", "description": "效率权衡，行驶减少但等待使总作业增加", "route_id": run["route_id"],
        "source_type": "saved_planning_snapshot", "run_id": run["run_id"],
        "task": {field: int(metadata[0][field]) for field in ("stop_count", "package_count", "valid_time_window_count")},
        "historical": {field: run["historical_metrics"].get(field) for field in METRICS},
        "suggested": {field: run["suggested_metrics"].get(field) for field in METRICS},
        "new_risk": {"package_count": None, "stop_count": None}, "review_status": review["decision"],
        "algorithm": {"name": run["algorithm"], "search_budget_seconds": run["search_budget_seconds"],
                      "parameters": run["parameters"], "solver_status": run["solver_status"]},
        "provenance": {"files": [source_file(root, p) for p in (db_path, index_path)],
                       "locator": f"planning_runs + planning_reviews WHERE run_id={RUN_ID}", "evidence_sha256": ""},
        "missing_data": ["new_risk: package and stop identities were not saved; totals are retained"],
    }
    # Fingerprint a public projection, never an entire private database record.
    b["provenance"]["evidence_sha256"] = sha256(encode({key: b[key] for key in ("run_id", "route_id", "historical", "suggested", "review_status")})).hexdigest()
    cases.append(b)
    for case in cases:
        case["simulation"] = simulation_protocol()
        case["comparisons"] = {
            field: {"historical": case["historical"][field], "suggested": case["suggested"][field],
                    **comparison(case["historical"][field], case["suggested"][field]),
                    "unit": "seconds" if field in TIME_FIELDS else "packages" if field == "risk_package_count" else "stops",
                    "lower_is_better": True}
            for field in METRICS
        }
        validate_case(case)
    return sorted(cases, key=lambda case: case["demo_case_id"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--package-dir", type=Path, default=ROOT / "demo_data")
    parser.add_argument("--check-source", action="store_true", help="Compare against saved sources without writing")
    args = parser.parse_args()
    cases = build_cases(args.source_root)
    if args.check_source:
        if load_package(args.package_dir) != cases:
            raise ValueError("Public package differs from saved source projection")
        print("All 3 cases exactly match saved evidence")
        return
    args.package_dir.joinpath("cases").mkdir(parents=True, exist_ok=True)
    entries = []
    for case in cases:
        path = f"cases/{case['demo_case_id']}.json"
        data = encode(case)
        (args.package_dir / path).write_bytes(data)
        entries.append({"demo_case_id": case["demo_case_id"], "path": path, "sha256": sha256(data).hexdigest()})
    manifest = {
        "version": "D1.1", "scope": "read_only_precomputed_offline_simulation",
        "license": "CC-BY-NC-4.0", "attribution": "2021 Amazon Last Mile Routing Research Challenge Dataset; Copyright Amazon.com, Inc. or its affiliates",
        "source_url": "https://registry.opendata.aws/amazon-last-mile-challenges/",
        "selection": "Three curated demonstration cases; not a replacement for the fixed 12-route evaluation",
        "tolerance": "0.000001", "cases": entries,
    }
    (args.package_dir / "manifest.json").write_bytes(encode(manifest))
    print(f"Built {len(load_package(args.package_dir))} verified demonstration cases")


if __name__ == "__main__":
    main()
