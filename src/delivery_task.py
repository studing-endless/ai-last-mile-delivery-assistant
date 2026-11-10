"""Assemble a delivery task from the validated Amazon route inputs."""

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from .data_loader import load_route_and_sequence
from .package_loader import load_route_packages
from .travel_time_loader import load_travel_matrix


def load_delivery_task(route_id: str, data_dir: Path) -> dict:
    """Return a Python task with route details, a directed matrix and package constraints."""
    route = load_route_and_sequence(route_id, data_dir)
    raw_date = route["date_YYYY_MM_DD"]
    raw_time = route["departure_time_utc"]
    try:
        delivery_date = date.fromisoformat(raw_date)
        departure_time = time.fromisoformat(raw_time)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"Route {route_id!r} has invalid departure metadata "
            f"(date_YYYY_MM_DD={raw_date!r}, departure_time_utc={raw_time!r})"
        ) from error
    if departure_time.utcoffset() not in (None, timedelta(0)):
        raise ValueError(f"Route {route_id!r} departure_time_utc must be UTC: {raw_time!r}")

    stop_ids = route["stops"].keys()
    travel_matrix = load_travel_matrix(route_id, data_dir, stop_ids)
    package_data = load_route_packages(route_id, data_dir, stop_ids)
    packages_by_stop = package_data["stops"]

    if package_data["route_id"] != route["route_id"]:
        raise ValueError(f"Route {route_id!r} received package data for {package_data['route_id']!r}")
    if travel_matrix.keys() != stop_ids or packages_by_stop.keys() != stop_ids:
        raise ValueError(f"Route {route_id!r} stop IDs do not match its travel matrix and package summaries")

    return {
        **route,
        "delivery_date": delivery_date,
        "departure_datetime_utc": datetime.combine(delivery_date, departure_time, tzinfo=UTC),
        "station_id": route["ordered_stops"][0],
        "travel_matrix": travel_matrix,
        "packages_by_stop": packages_by_stop,
    }
