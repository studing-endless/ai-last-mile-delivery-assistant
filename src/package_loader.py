"""Read package records and summarize their delivery constraints by stop."""

import json
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from math import fsum, isnan
from pathlib import Path
from sys import float_info
from typing import TextIO

_BLOCK_SIZE = 64 * 1024
_JSON_WHITESPACE = " \t\r\n"
_STRUCTURE = re.compile(r'\\[\s\S]?|["{}\[\],]')
_TimeWindow = tuple[datetime, datetime]


def _route_members(source: TextIO) -> Iterator[str]:
    """Split a top-level object into members, buffering only one route at a time."""
    depth = 0
    in_string = False
    escaped = False
    pieces = []
    for block in iter(lambda: source.read(_BLOCK_SIZE), ""):
        start = 0
        if depth == 0:
            block = block.lstrip(_JSON_WHITESPACE)
            if not block:
                continue
            if block[0] != "{":
                raise ValueError("Expected a top-level JSON object")
            depth = 1
            start = 1

        scan_from = start + int(escaped)
        escaped = False
        for match in _STRUCTURE.finditer(block, scan_from):
            token = match[0]
            if token.startswith("\\"):
                escaped = len(token) == 1
            elif token == '"':
                in_string = not in_string
            elif not in_string:
                if token in ("{", "["):
                    depth += 1
                elif token in ("}", "]"):
                    depth -= 1

                if depth == 0 or (depth == 1 and token == ","):
                    pieces.append(block[start:match.start()])
                    yield "".join(pieces)
                    if depth == 0:
                        return
                    pieces.clear()
                    start = match.end()

        pieces.append(block[start:])

    raise ValueError("Incomplete top-level JSON object")


def iter_route_objects(path: Path, route_ids: Iterable[str] | None = None) -> Iterator[tuple[str, object]]:
    """Stream top-level Amazon JSON objects, including files that contain NaN."""
    decoder = json.JSONDecoder()
    pending = None if route_ids is None else set(route_ids)
    if pending == set():
        return
    with path.open(encoding="utf-8") as source:
        for member in _route_members(source):
            member = member.lstrip(_JSON_WHITESPACE)
            if not member:
                continue
            key, end = decoder.raw_decode(member)
            if not isinstance(key, str):
                raise ValueError("Expected a top-level route ID string")
            if pending is not None and key not in pending:
                continue
            separator = member[end:].lstrip(_JSON_WHITESPACE)
            if not separator.startswith(":"):
                raise ValueError("Expected ':' after the route ID")
            yield key, decoder.decode(separator[1:])
            if pending is not None:
                pending.remove(key)
                if not pending:
                    return


def _read_route_packages(path: Path, route_id: str) -> dict:
    try:
        for _, packages in iter_route_objects(path, (route_id,)):
            return packages
    except ValueError as error:
        raise ValueError(f"Could not read route {route_id!r} from {path.name}: {error}") from error

    raise KeyError(f"Route {route_id!r} not found in {path.name}")


def _missing(value: object) -> bool:
    return value is None or (isinstance(value, float) and isnan(value))


def _parse_utc(value: object, field: str, context: str) -> datetime | None:
    if _missing(value) or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str):
        text = value.strip()
        if " " in text or "T" in text:
            try:
                parsed = datetime.fromisoformat(text)
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=UTC)
                return parsed.astimezone(UTC)
            except (ValueError, OverflowError) as error:
                raise ValueError(f"{context} has invalid {field}: {value!r}") from error
    raise ValueError(f"{context} has invalid {field}: {value!r}")


def parse_time_window(raw: object, context: str) -> _TimeWindow | None:
    """Validate a package's window and convert its endpoints to UTC."""
    if _missing(raw):
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{context} time_window must be an object or missing")

    start = _parse_utc(raw.get("start_time_utc"), "start_time_utc", context)
    end = _parse_utc(raw.get("end_time_utc"), "end_time_utc", context)
    if start is None and end is None:
        return None
    if start is None or end is None:
        raise ValueError(f"{context} has an incomplete time window")
    if start >= end:
        raise ValueError(f"{context} time window start must be before its end")
    return start, end


def load_route_packages(route_id: str, data_dir: Path, stop_ids: Iterable[str]) -> dict:
    """Return the route ID and package records and constraints for each supplied stop.

    Parsed windows are inclusive (start, end) pairs in UTC. Service totals are
    sums of package plans, distinct from actual time spent at a stop.
    """
    raw_stops = _read_route_packages(data_dir / "package_data.json", route_id)
    if not isinstance(raw_stops, dict):
        raise ValueError(f"Route {route_id!r} package data must be an object")

    expected_stops = dict.fromkeys(stop_ids)
    unknown = raw_stops.keys() - expected_stops.keys()
    if unknown:
        raise ValueError(f"Route {route_id!r} package data contains unknown stops: {sorted(unknown)}")

    stops = {}
    for stop_id in expected_stops:
        raw_packages = raw_stops.get(stop_id, {})
        if not isinstance(raw_packages, dict):
            raise ValueError(f"Route {route_id!r} stop {stop_id!r} packages must be an object")

        packages = {}
        windows = {}
        for package_id, info in raw_packages.items():
            context = f"Route {route_id!r} stop {stop_id!r} package {package_id!r}"
            if not isinstance(info, dict):
                raise ValueError(f"{context} record must be an object")

            service = info.get("planned_service_time_seconds")
            if type(service) not in (int, float) or not 0 <= service <= float_info.max:
                raise ValueError(f"{context} planned service time must be finite and non-negative: {service!r}")

            raw_window = info.get("time_window")
            window = parse_time_window(raw_window, context)
            packages[package_id] = {
                "package_id": package_id,
                "scan_status": info.get("scan_status"),
                "planned_service_time_seconds": service,
                "dimensions": info.get("dimensions"),
                "raw_time_window": raw_window,
                "time_window": window,
            }
            if window is not None:
                windows[package_id] = window

        intersection = None
        empty_intersection = False
        if windows:
            latest_start = max(start for start, _ in windows.values())
            earliest_end = min(end for _, end in windows.values())
            empty_intersection = latest_start > earliest_end
            if not empty_intersection:
                intersection = latest_start, earliest_end

        stops[stop_id] = {
            "packages": packages,
            "package_count": len(packages),
            "planned_service_time_seconds_sum": fsum(
                package["planned_service_time_seconds"] for package in packages.values()
            ),
            "package_time_windows": windows,
            "time_window_intersection": intersection,
            "has_empty_time_window_intersection": empty_intersection,
        }

    return {"route_id": route_id, "stops": stops}
