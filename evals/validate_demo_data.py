"""Validate the public D1 package using only the Python standard library."""

import argparse
from decimal import Decimal
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import re


TIME_FIELDS = ("driving_seconds", "planned_service_time_seconds_sum", "waiting_seconds", "total_operation_seconds")
COUNT_FIELDS = ("risk_package_count", "risk_stop_count", "window_conflict_stop_count")
METRICS = TIME_FIELDS + COUNT_FIELDS
TOLERANCE = Decimal("0.000001")
DEFAULT_PACKAGE = Path(__file__).resolve().parents[1] / "demo_data"


def read_json(path):
    def reject(value):
        raise ValueError(f"Nonstandard JSON constant: {value}")

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON field: {key}")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject, object_pairs_hook=unique_pairs)


def keys(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError(f"Unexpected or missing fields; expected {tuple(expected)}")


def number(value, *, count=False):
    if type(value) not in (int, float) or not isfinite(value) or value < 0 or (count and type(value) is not int):
        raise ValueError("Expected finite nonnegative number or integer count")


def close(actual, expected):
    if expected is None:
        if actual is not None:
            raise ValueError("Missing comparison must be null")
    elif type(actual) not in (int, float) or not isfinite(actual) or abs(Decimal(str(actual)) - expected) > TOLERANCE:
        raise ValueError("Inconsistent numeric comparison")


def comparison(historical, suggested):
    if historical is None or suggested is None:
        return {"delta": None, "change_percent": None, "direction": None}
    delta = Decimal(str(suggested)) - Decimal(str(historical))
    return {
        "delta": float(delta),
        "change_percent": float(delta / Decimal(str(historical)) * 100) if historical else None,
        "direction": "decreased" if delta < 0 else "increased" if delta > 0 else "unchanged",
    }


def validate_case(case):
    keys(case, ("demo_case_id", "description", "route_id", "source_type", "run_id", "task", "historical", "suggested",
                "comparisons", "new_risk", "review_status", "algorithm", "simulation", "provenance", "missing_data"))
    if case["demo_case_id"] not in ("A", "B", "C") or not isinstance(case["description"], str):
        raise ValueError("Invalid demo case identity")
    if not re.fullmatch(r"RouteID_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", case["route_id"]):
        raise ValueError("Invalid Amazon Route ID")
    if case["source_type"] not in ("saved_planning_snapshot", "m6_4_evaluation"):
        raise ValueError("Invalid source type")
    if case["run_id"] is not None and not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", case["run_id"]):
        raise ValueError("Invalid run ID")
    if (case["source_type"] == "saved_planning_snapshot") != (case["run_id"] is not None):
        raise ValueError("Run ID must come from an existing planning snapshot")
    keys(case["task"], ("stop_count", "package_count", "valid_time_window_count"))
    for value in case["task"].values():
        if value is not None:
            number(value, count=True)
    for side in ("historical", "suggested"):
        keys(case[side], METRICS)
        for field, value in case[side].items():
            if value is not None:
                number(value, count=field in COUNT_FIELDS)
        if all(case[side][field] is not None for field in TIME_FIELDS):
            close(case[side]["total_operation_seconds"], sum(Decimal(str(case[side][field])) for field in TIME_FIELDS[:3]))
    keys(case["comparisons"], METRICS)
    for field, saved in case["comparisons"].items():
        keys(saved, ("historical", "suggested", "delta", "change_percent", "direction", "unit", "lower_is_better"))
        for side in ("historical", "suggested"):
            if saved[side] != case[side][field]:
                raise ValueError("Comparison differs from stored metric")
        expected = comparison(saved["historical"], saved["suggested"])
        for name in ("delta", "change_percent"):
            close(saved[name], Decimal(str(expected[name])) if expected[name] is not None else None)
        unit = "seconds" if field in TIME_FIELDS else "packages" if field == "risk_package_count" else "stops"
        if saved["direction"] != expected["direction"] or saved["unit"] != unit or saved["lower_is_better"] is not True:
            raise ValueError("Invalid comparison semantics")
    keys(case["new_risk"], ("package_count", "stop_count"))
    for kind, value in case["new_risk"].items():
        if value is not None:
            number(value, count=True)
            field = "risk_package_count" if kind == "package_count" else "risk_stop_count"
            h, s = case["historical"][field], case["suggested"][field]
            if h is not None and s is not None and not max(0, s - h) <= value <= s:
                raise ValueError("Invalid newly risky count")
    if case["review_status"] not in (None, "PENDING", "APPROVED", "REJECTED"):
        raise ValueError("Invalid saved review status")
    keys(case["algorithm"], ("name", "search_budget_seconds", "parameters", "solver_status"))
    number(case["algorithm"]["search_budget_seconds"])
    keys(case["algorithm"]["parameters"], ("vehicles", "objective", "first_solution_strategy", "local_search_metaheuristic", "cost_scale"))
    if case["algorithm"]["search_budget_seconds"] != 20 or case["algorithm"]["parameters"]["vehicles"] != 1:
        raise ValueError("Unexpected planning protocol")
    if case["simulation"] != simulation_protocol():
        raise ValueError("Unexpected simulation semantics")
    keys(case["provenance"], ("files", "locator", "evidence_sha256"))
    for source in case["provenance"]["files"]:
        keys(source, ("path", "sha256"))
        if Path(source["path"]).is_absolute() or ".." in Path(source["path"]).parts or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"]):
            raise ValueError("Invalid source fingerprint")
    if not re.fullmatch(r"[0-9a-f]{64}", case["provenance"]["evidence_sha256"]):
        raise ValueError("Invalid evidence fingerprint")
    if not isinstance(case["missing_data"], list) or not all(isinstance(v, str) for v in case["missing_data"]):
        raise ValueError("Invalid missing-data declaration")
    text = json.dumps(case, ensure_ascii=False, allow_nan=False)
    if re.search(r"(?i)authorization|api[_ -]?key|bearer\s|sk-[a-z0-9_-]{16,}|-----BEGIN .*PRIVATE KEY|[A-Z]:[\\/]|/Users/|/home/", text):
        raise ValueError("Potential credential or private path in public case")
    d = case["comparisons"]
    if any(d[field]["delta"] is None for field in ("driving_seconds", "waiting_seconds", "total_operation_seconds")):
        raise ValueError("Case classification cannot be confirmed with missing time evidence")
    if case["demo_case_id"] == "A":
        valid = d["driving_seconds"]["delta"] < 0 and d["total_operation_seconds"]["delta"] < 0 and case["new_risk"] == {"package_count": 0, "stop_count": 0}
    elif case["demo_case_id"] == "B":
        valid = d["driving_seconds"]["delta"] < 0 and d["total_operation_seconds"]["delta"] > 0 and d["waiting_seconds"]["delta"] > 0
    else:
        valid = d["total_operation_seconds"]["delta"] < 0 and case["new_risk"]["package_count"] > 0 and case["new_risk"]["stop_count"] > 0
    if not valid:
        raise ValueError("Case type disagrees with actual metrics")


def simulation_protocol():
    return {
        "scope": "offline_simulation_estimate", "tour": "same_depot_closed_tour",
        "travel": "same_original_directed_seconds", "departure": "same_utc_departure",
        "service": "sum_of_package_planned_service_seconds_not_actual_dwell",
        "windows": "inclusive_window_check_at_station_service_start",
        "delta": "suggested_minus_historical", "percent": "delta / historical * 100; null if historical is zero",
    }


def load_package(directory=DEFAULT_PACKAGE):
    directory = Path(directory)
    manifest = read_json(directory / "manifest.json")
    keys(manifest, ("version", "scope", "license", "attribution", "source_url", "selection", "tolerance", "cases"))
    if manifest["version"] != "D1.1" or manifest["license"] != "CC-BY-NC-4.0" or manifest["tolerance"] != str(TOLERANCE):
        raise ValueError("Unsupported package protocol")
    cases = []
    for entry in manifest["cases"]:
        keys(entry, ("demo_case_id", "path", "sha256"))
        if entry["path"] != f"cases/{entry['demo_case_id']}.json":
            raise ValueError("Invalid case file path")
        path = directory / entry["path"]
        if sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("Case file fingerprint mismatch")
        case = read_json(path)
        validate_case(case)
        if case["demo_case_id"] != entry["demo_case_id"]:
            raise ValueError("Case ID mismatch")
        cases.append(case)
    if [case["demo_case_id"] for case in cases] != ["A", "B", "C"]:
        raise ValueError("Expected exactly three ordered cases A, B, C")
    allowed = {"manifest.json", "README.md", *(entry["path"] for entry in manifest["cases"])}
    if any(path.relative_to(directory).as_posix() not in allowed for path in directory.rglob("*") if path.is_file()):
        raise ValueError("Unexpected file in public package")
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-dir", type=Path, default=DEFAULT_PACKAGE)
    args = parser.parse_args()
    print(f"Validated {len(load_package(args.package_dir))} offline demonstration cases")


if __name__ == "__main__":
    main()
