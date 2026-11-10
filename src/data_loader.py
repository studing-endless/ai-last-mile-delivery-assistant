"""Load a route and its observed stop order from Amazon delivery data."""

import json
from pathlib import Path


def _load_route_record(path: Path, route_id: str) -> dict:
    with path.open(encoding="utf-8") as source:
        records = json.load(source)

    if route_id not in records:
        raise KeyError(f"Route {route_id!r} not found in {path.name}")
    return records[route_id]


def load_route_and_sequence(route_id: str, data_dir: Path) -> dict:
    """Read a route and its validated actual stop order from the raw data directory."""
    route = _load_route_record(data_dir / "route_data.json", route_id)
    sequence = _load_route_record(data_dir / "actual_sequences.json", route_id)
    stops = route.get("stops")
    actual = sequence.get("actual")

    if not isinstance(stops, dict) or not isinstance(actual, dict):
        raise ValueError(f"Route {route_id!r} requires 'stops' and 'actual' mappings")

    if actual.keys() != stops.keys():
        missing = sorted(stops.keys() - actual.keys())
        extra = sorted(actual.keys() - stops.keys())
        raise ValueError(f"Route {route_id!r} stop IDs do not match: missing={missing}, extra={extra}")

    stop_count = len(stops)
    positions = actual.values()
    if any(type(position) is not int for position in positions) or set(positions) != set(range(stop_count)):
        raise ValueError(f"Route {route_id!r} sequence positions must be consecutive integers from 0 to N-1")

    station_count = 0
    for stop_id, stop in stops.items():
        stop_type = stop.get("type") if isinstance(stop, dict) else None
        if stop_type == "Station":
            station_count += 1
        elif stop_type != "Dropoff":
            raise ValueError(
                f"Route {route_id!r} stop {stop_id!r} has invalid type {stop_type!r}. "
                "Expected 'Station' or 'Dropoff'."
            )

    if station_count != 1:
        raise ValueError(
            f"Route {route_id!r} must contain exactly one 'Station' stop (found {station_count})"
        )

    ordered_stops = sorted(actual, key=actual.__getitem__)
    if stops[ordered_stops[0]]["type"] != "Station":
        raise ValueError(f"Route {route_id!r} must start at a stop of type 'Station'")

    return {
        "route_id": route_id,
        "station_code": route["station_code"],
        "date_YYYY_MM_DD": route.get("date_YYYY_MM_DD"),
        "departure_time_utc": route.get("departure_time_utc"),
        "stops": stops,
        "ordered_stops": ordered_stops,
        "stop_count": stop_count,
        "station_count": station_count,
        "dropoff_count": stop_count - station_count,
    }
