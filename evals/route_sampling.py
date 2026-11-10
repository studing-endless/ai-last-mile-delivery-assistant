"""Freeze a stratified sample from Amazon route and package metadata."""

import csv
import hashlib
import json
import random
from collections import Counter
from pathlib import Path

from src.package_loader import iter_route_objects, parse_time_window


SEED = 20261110
EXPECTED_ROUTE_COUNT = 6112
STOP_BINS = ("<=100", "101-150", ">=151")
WINDOW_BINS = ("0", "1-15", "16-40", ">=41")
STRATA = tuple((stop, (column + offset) % 4) for column in range(4) for stop, offset in enumerate((0, 1, 3)))
INDEX_FIELDS = (
    "route_id", "stop_count", "package_count", "valid_time_window_count", "invalid_time_window_count",
    "route_score", "metadata_status",
)
SOURCE_FILES = ("route_data.json", "package_data.json")


def build_metadata_index(data_dir: Path) -> tuple[list[dict], int]:
    metadata = {}
    for route_id, route in iter_route_objects(data_dir / "route_data.json"):
        if route_id in metadata:
            raise ValueError(f"Duplicate route ID in route_data.json: {route_id}")
        stops = route.get("stops") if isinstance(route, dict) else None
        score = route.get("route_score") if isinstance(route, dict) else None
        metadata[route_id] = {
            "route_id": route_id,
            "stop_count": len(stops) if isinstance(stops, dict) else None,
            "package_count": None, "valid_time_window_count": None, "invalid_time_window_count": None,
            "route_score": score if isinstance(score, str) else None,
            "metadata_status": "missing_package_route",
        }

    extra_routes = 0
    seen = set()
    for route_id, stops in iter_route_objects(data_dir / "package_data.json"):
        if route_id in seen:
            raise ValueError(f"Duplicate route ID in package_data.json: {route_id}")
        seen.add(route_id)
        if route_id not in metadata:
            extra_routes += 1
            continue
        row = metadata[route_id]
        if not isinstance(stops, dict) or any(not isinstance(packages, dict) for packages in stops.values()):
            row["metadata_status"] = "invalid_package_structure"
            continue
        valid = invalid = 0
        for packages in stops.values():
            for package_id, package in packages.items():
                try:
                    if not isinstance(package, dict):
                        raise ValueError("Invalid package record")
                    valid += parse_time_window(package.get("time_window"), f"{route_id}/{package_id}") is not None
                except ValueError:
                    invalid += 1
        row.update(
            package_count=sum(len(packages) for packages in stops.values()),
            valid_time_window_count=valid,
            invalid_time_window_count=invalid,
            metadata_status="invalid_stop_metadata" if row["stop_count"] is None else
            "invalid_time_windows" if invalid else "indexed",
        )
    return [metadata[route_id] for route_id in sorted(metadata)], extra_routes


def stratum(row: dict) -> tuple[int, int]:
    stops, windows = row["stop_count"], row["valid_time_window_count"]
    return (int(stops > 100) + int(stops > 150), int(windows > 0) + int(windows > 15) + int(windows > 40))


def stratum_label(cell: tuple[int, int]) -> dict:
    stop, window = cell
    return {"stop_count": STOP_BINS[stop], "valid_time_window_count": WINDOW_BINS[window]}


def select_samples(index: list[dict]) -> tuple[list[dict], list[dict]]:
    candidates = {cell: [] for cell in STRATA}
    for row in sorted(index, key=lambda row: row["route_id"]):
        if row["stop_count"] is not None and row["valid_time_window_count"] is not None:
            candidates[stratum(row)].append(row)
    if sum(map(len, candidates.values())) < len(STRATA):
        raise ValueError("Fewer than 12 routes have usable stratification metadata")
    counts = [{**stratum_label(cell), "candidate_count": len(candidates[cell])} for cell in STRATA]

    rng = random.Random(SEED)
    selected = {}
    for cell in STRATA:
        if pool := candidates[cell]:
            choice = rng.choice(pool)
            pool.remove(choice)
            selected[cell] = (cell, choice)
    for cell in STRATA:
        if cell in selected:
            continue
        donor = min(
            (other for other, pool in candidates.items() if pool),
            key=lambda other: (abs(cell[0] - other[0]) + abs(cell[1] - other[1]), other),
        )
        choice = rng.choice(candidates[donor])
        candidates[donor].remove(choice)
        selected[cell] = (donor, choice)

    samples = []
    for position, cell in enumerate(STRATA, start=1):
        donor, row = selected[cell]
        samples.append({
            "position": position, "target_stratum": stratum_label(cell), "actual_stratum": stratum_label(donor),
            **row,
        })
    return samples, counts


def freeze_sample(data_dir: Path, manifest_path: Path, index_path: Path) -> dict:
    if manifest_path.exists() or index_path.exists():
        raise FileExistsError("The sample manifest or metadata index already exists")
    index, extra_routes = build_metadata_index(data_dir)
    if len(index) != EXPECTED_ROUTE_COUNT:
        raise ValueError(f"Expected {EXPECTED_ROUTE_COUNT} Amazon routes, found {len(index)}")
    samples, counts = select_samples(index)
    sources = {}
    for name in SOURCE_FILES:
        path = data_dir / name
        with path.open("rb") as source:
            sources[name] = {"size_bytes": path.stat().st_size, "sha256": hashlib.file_digest(source, "sha256").hexdigest()}
    manifest = {
        "version": "m6-4a.1",
        "seed": SEED,
        "source_files": sources,
        "metadata_index": {
            "filename": index_path.name,
            "route_count": len(index),
            "statuses": dict(Counter(row["metadata_status"] for row in index)),
            "unmatched_package_route_count": extra_routes,
        },
        "selection_rules": {
            "stop_count": "All route stops, including the depot",
            "valid_time_window_count": "Packages with two parseable UTC endpoints and start before end",
            "quality_label": "Raw route_score, descriptive only",
            "eligibility": "Known stop and valid-window counts. Invalid individual windows remain eligible and are counted separately",
            "sampling": "One per nonempty stratum via random.Random(seed).choice on ascending Route ID candidates",
            "order": "Traverse four window-bin columns across the three size bins, with window offsets 0, 1, 3",
            "fallback": "After reserving nonempty strata, fill empty strata from the nearest unused pool by Manhattan bin distance. Break distance ties by ascending stop-bin then window-bin index",
            "pilot": "First three samples in manifest order",
        },
        "strata": counts,
        "samples": samples,
    }
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("x", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=INDEX_FIELDS)
        writer.writeheader()
        writer.writerows(index)
    with manifest_path.open("x", encoding="utf-8") as file:
        json.dump(manifest, file, ensure_ascii=False, allow_nan=False, indent=2)
        file.write("\n")
    return manifest
