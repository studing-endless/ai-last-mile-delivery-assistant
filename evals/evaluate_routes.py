"""Run the same offline planning evaluation for a slice of a frozen Amazon sample."""

import argparse
import csv
import hashlib
import json
from datetime import UTC, datetime
from math import isclose, isfinite
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

import ijson
import ortools

from evals.route_sampling import freeze_sample
from src.delivery_task import load_delivery_task
from src.package_loader import iter_route_objects
from src.route_optimizer import optimize_route
from src.schedule_evaluator import evaluate_schedule


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "evals" / "route_sample_manifest.json"
OUTPUT_ROOT = ROOT / "outputs" / "route_eval"
INPUT_FILES = ("route_data.json", "actual_sequences.json", "package_data.json", "travel_times.json")
SEARCH_SECONDS = 20
METRICS = (
    "driving_seconds", "total_operation_seconds", "waiting_seconds", "planned_service_time_seconds_sum",
    "risk_package_count", "risk_stop_count", "window_conflict_stop_count",
)
FIELDS = (
    "position", "route_id", "route_score", "stop_count", "package_count", "valid_time_window_count",
    "data_validation_status", "solve_status", "solver_status", "found_solution", "tour_verified",
    "data_load_seconds", "solve_seconds", "solver_elapsed_seconds", "evaluation_elapsed_seconds",
    *(f"{side}_{metric}" for side in ("historical", "suggested") for metric in METRICS),
    "new_risk_package_count", "new_risk_stop_count",
    "driving_change_percent", "total_operation_change_percent", "failure_stage", "failure_reason",
)


def evaluation_protocol() -> dict:
    return {
        "solver": "OR-Tools RoutingModel", "ortools_version": ortools.__version__, "vehicle_count": 1,
        "first_solution_strategy": "PATH_CHEAPEST_ARC", "local_search_metaheuristic": "GUIDED_LOCAL_SEARCH",
        "search_budget_seconds": SEARCH_SECONDS, "objective": "minimize directed driving time",
        "cost_unit": "milliseconds, round each leg to nearest with ties to even",
        "tour": "start and end at the same depot, visit each dropoff once",
        "schedule": "same directed matrix, UTC departure and planned package service for both tours",
        "window_risk": "packages outside their inclusive windows at simulated stop service start",
        "percent_change": "(suggested - historical) / historical * 100. Null for a zero baseline",
        "scope": "offline simulation estimates",
    }


def travel_matrices(path: Path, route_ids: set[str]):
    """Scan once with bounded memory and retain only the selected matrices."""
    pending = route_ids.copy()
    with path.open("rb") as source:
        for route_id, matrix in ijson.kvitems(source, "", use_float=True):
            if route_id in pending:
                yield route_id, matrix
                pending.remove(route_id)
                if not pending:
                    return


def stage_inputs(data_dir: Path, stage_dir: Path, route_ids: set[str]) -> dict:
    """Stage only the requested routes for the existing delivery task loader."""
    inputs = {}
    for name in INPUT_FILES:
        started = perf_counter()
        error = None
        selected = {}
        path = data_dir / name
        try:
            rows = travel_matrices(path, route_ids) if name == "travel_times.json" else iter_route_objects(path, route_ids)
            for route_id, record in rows:
                selected[route_id] = record
        except (OSError, ValueError, ijson.JSONError) as exc:
            error = f"{type(exc).__name__}: {exc}"
        with (stage_dir / name).open("w", encoding="utf-8") as file:
            # Route and package snapshots may contain Amazon's NaN markers.
            json.dump(selected, file, ensure_ascii=False, allow_nan=name != "travel_times.json")
        inputs[name] = {
            "elapsed_seconds": perf_counter() - started,
            "routes_extracted": len(selected),
            "missing_route_ids": sorted(route_ids - selected.keys()),
            "failure_reason": error,
        }
    return inputs


def record_metrics(row: dict, side: str, schedule: dict) -> None:
    metrics = {f"{side}_{key}": schedule[key] for key in METRICS}
    if not all(isfinite(value) and value >= 0 for value in metrics.values()):
        raise ValueError(f"{side} schedule contains a non-finite or negative metric")
    total = schedule["driving_seconds"] + schedule["waiting_seconds"] + schedule["planned_service_time_seconds_sum"]
    if not isclose(total, schedule["total_operation_seconds"], rel_tol=1e-12, abs_tol=1e-6):
        raise ValueError(f"{side} total operation time disagrees with its components")
    row.update(metrics)


def change_percent(historical: float, suggested: float) -> float | None:
    return (suggested - historical) / historical * 100 if historical else None


def risk_pairs(schedule: dict) -> set[tuple[str, str]]:
    return {
        (stop_id, package_id)
        for stop_id, details in schedule["stop_details"].items()
        for package_id in details["window_violations"]
    }


def evaluate_sample(sample: dict, data_dir: Path) -> dict:
    started = perf_counter()
    row = dict.fromkeys(FIELDS)
    row.update({key: sample[key] for key in (
        "position", "route_id", "route_score", "stop_count", "package_count", "valid_time_window_count",
    )})
    row.update(data_validation_status="failed", solve_status="not_attempted")
    phase = "data_load"
    try:
        try:
            task = load_delivery_task(row["route_id"], data_dir)
        finally:
            row["data_load_seconds"] = perf_counter() - started
        stops = task["packages_by_stop"].values()
        counts = {
            "stop_count": task["stop_count"],
            "package_count": sum(stop["package_count"] for stop in stops),
            "valid_time_window_count": sum(len(stop["package_time_windows"]) for stop in stops),
        }
        if any(sample[key] != count for key, count in counts.items()):
            raise ValueError(f"Validated counts differ from the frozen manifest: {counts}")
        row["data_validation_status"] = "passed"

        phase = "historical_simulation"
        # The evaluator closes the historical tour using the same return-leg convention.
        historical = evaluate_schedule(task, task["ordered_stops"])
        record_metrics(row, "historical", historical)

        phase = "solve"
        row["solve_status"] = "failed"
        solving = perf_counter()
        try:
            solution = optimize_route(task, time_limit_seconds=SEARCH_SECONDS)
        finally:
            row["solver_elapsed_seconds"] = perf_counter() - solving
        row.update(
            solver_status=solution["status"], found_solution=solution["found_solution"], solve_seconds=solution["solve_seconds"],
        )
        if not solution["found_solution"]:
            raise RuntimeError(f"No route returned by the solver ({solution['status']})")
        row["solve_status"] = "succeeded"

        phase = "suggested_simulation"
        row["tour_verified"] = False
        order = solution["suggested_order"]
        station = task["station_id"]
        if not order or order[0] != station or order[-1] != station:
            raise ValueError("Suggested tour does not start and end at the depot")
        suggested = evaluate_schedule(task, order)
        row["tour_verified"] = True
        if not isclose(historical["driving_seconds"], solution["historical_driving_seconds"], abs_tol=1e-6) or not isclose(
            suggested["driving_seconds"], solution["suggested_driving_seconds"], abs_tol=1e-6,
        ):
            raise ValueError("Solver and simulator disagree on driving time")
        changes = {
            f"{metric.removesuffix('_seconds')}_change_percent": change_percent(historical[metric], suggested[metric])
            for metric in ("driving_seconds", "total_operation_seconds")
        }
        if any(value is not None and not isfinite(value) for value in changes.values()):
            raise ValueError("A percentage change is not finite")
        historical_risks, suggested_risks = risk_pairs(historical), risk_pairs(suggested)
        record_metrics(row, "suggested", suggested)
        row["new_risk_package_count"] = len(suggested_risks - historical_risks)
        row["new_risk_stop_count"] = len({stop for stop, _ in suggested_risks} - {stop for stop, _ in historical_risks})
        row.update(changes)
    except Exception as exc:
        row.update(failure_stage=phase, failure_reason=f"{type(exc).__name__}: {exc}")
    row["evaluation_elapsed_seconds"] = perf_counter() - started
    return row


def write_checkpoint(output_dir: Path, report: dict) -> None:
    temporary = output_dir / "results.json.tmp"
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, allow_nan=False, indent=2)
        file.write("\n")
    temporary.replace(output_dir / "results.json")


def write_csv(output_dir: Path, report: dict, fields: tuple[str, ...] = FIELDS) -> None:
    temporary = output_dir / "results.csv.tmp"
    with temporary.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(report["results"])
    temporary.replace(output_dir / "results.csv")


def write_results(output_dir: Path, report: dict, fields: tuple[str, ...] = FIELDS) -> None:
    output_dir.mkdir(parents=True, exist_ok=False)
    write_checkpoint(output_dir, report)
    write_csv(output_dir, report, fields)


def run_samples(selected: list[dict], data_dir: Path, output: Path, manifest_sha256: str, *, resume: bool = False) -> dict:
    started = perf_counter()
    source_files = {}
    for name in INPUT_FILES:
        path = data_dir / name
        try:
            stat = path.stat()
            source_files[name] = {"size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        except FileNotFoundError:
            source_files[name] = None
    identity = {
        "manifest_sha256": manifest_sha256,
        "requested_positions": [sample["position"] for sample in selected],
        "protocol": evaluation_protocol(),
        "source_dir": str(data_dir.resolve()),
        "source_files": source_files,
    }
    if resume:
        report = json.loads((output / "results.json").read_bytes())
        if any(report.get(key) != value for key, value in identity.items()):
            raise ValueError("Checkpoint does not match the requested sample, protocol or source files")
        completed = [(row["position"], row["route_id"]) for row in report["results"]]
        prefix = [(sample["position"], sample["route_id"]) for sample in selected[:len(completed)]]
        if completed != prefix or report["sample_positions"] != [position for position, _ in completed]:
            raise ValueError("Checkpoint rows do not match the ordered prefix of the frozen sample")
    else:
        output.mkdir(parents=True, exist_ok=False)
        report = {
            **identity,
            "started_at_utc": datetime.now(UTC).isoformat(), "elapsed_seconds": 0.0,
            "sample_positions": [], "staging": {}, "staging_attempts": [], "results": [],
        }
        write_checkpoint(output, report)

    pending = selected[len(report["results"]):]
    previous_elapsed = report["elapsed_seconds"]
    if pending:
        with TemporaryDirectory(prefix="route-evaluation-") as directory:
            stage_dir = Path(directory)
            print(f"Extracting {len(pending)} pending routes in one pass per raw file.", flush=True)
            staging = stage_inputs(data_dir, stage_dir, {sample["route_id"] for sample in pending})
            report["staging"] = staging
            report["staging_attempts"].append(staging)
            report["elapsed_seconds"] = previous_elapsed + perf_counter() - started
            write_checkpoint(output, report)
            for sample in pending:
                print(f"Evaluating sample {sample['position']}: {sample['route_id']}", flush=True)
                row = evaluate_sample(sample, stage_dir)
                report["results"].append(row)
                report["sample_positions"].append(row["position"])
                report["elapsed_seconds"] = previous_elapsed + perf_counter() - started
                write_checkpoint(output, report)
                if row["failure_reason"]:
                    print(f"  {row['failure_stage']} failed: {row['failure_reason']}", flush=True)
                else:
                    print(f"  Saved sample {row['position']} ({row['solve_seconds']:.2f} s solve)", flush=True)
    write_csv(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "raw")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("freeze", help="Create the sample manifest and metadata index once")
    run = commands.add_parser("run", help="Evaluate an explicit slice of the frozen manifest")
    run.add_argument("--start", type=int, default=1, help="First manifest position, starting at 1")
    run.add_argument("--limit", type=int, default=3, help="Number of consecutive samples")
    run.add_argument("--output-dir", type=Path, help="Directory for the batch results and checkpoint")
    run.add_argument("--resume", action="store_true", help="Continue an existing batch in --output-dir")
    args = parser.parse_args()

    if args.command == "freeze":
        index = OUTPUT_ROOT / "route_metadata_index.csv"
        manifest = freeze_sample(args.data_dir, MANIFEST, index)
        print(f"Froze {len(manifest['samples'])} samples from {manifest['metadata_index']['route_count']} indexed routes.")
        print(MANIFEST)
        return 0

    raw_manifest = MANIFEST.read_bytes()
    samples = json.loads(raw_manifest)["samples"]
    if len(samples) != 12 or len({sample["route_id"] for sample in samples}) != 12 or [
        sample["position"] for sample in samples
    ] != list(range(1, 13)):
        parser.error("Expected 12 unique ordered samples in the frozen manifest")
    if not 1 <= args.start <= len(samples) or not 1 <= args.limit <= len(samples) - args.start + 1:
        parser.error("Requested slice lies outside the sample manifest")
    selected = samples[args.start - 1:args.start - 1 + args.limit]
    if args.resume and args.output_dir is None:
        parser.error("--resume requires --output-dir")
    output = args.output_dir or OUTPUT_ROOT / f"run-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}"
    report = run_samples(selected, args.data_dir, output, hashlib.sha256(raw_manifest).hexdigest(), resume=args.resume)
    print(output)
    return int(any(row["failure_reason"] for row in report["results"])
               or any(item["failure_reason"] for item in report["staging"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
