"""Simulate the UTC schedule of a fixed delivery order."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal


def _seconds(delta: timedelta) -> Decimal:
    return Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds).scaleb(-6)


def _timestamp(departure: datetime, offset: Decimal) -> datetime:
    # Only the reported datetime is quantized to Python's microsecond resolution.
    return departure + timedelta(microseconds=round(offset * 1_000_000))


def evaluate_schedule(delivery_task: dict, ordered_stops: Sequence[str]) -> dict:
    """Simulate a full order, with or without a trailing depot in the input.

    The depot marks fixed departure and return. Each dropoff's summed package
    plan is simulated as one service block. Its start is a proxy for checking
    each package's inclusive window. For conflicting windows, service starts at
    arrival and each package window is still checked.
    """
    station = delivery_task["station_id"]
    if not ordered_stops or ordered_stops[0] != station:
        raise ValueError(f"Visit order must start at station {station!r}")
    visits = ordered_stops[1:]
    if visits and visits[-1] == station:
        visits = visits[:-1]
    dropoffs = delivery_task["stops"].keys() - {station}
    if len(visits) != len(dropoffs) or set(visits) != dropoffs:
        raise ValueError("Visit order must include each dropoff exactly once")

    departure = delivery_task["departure_datetime_utc"]
    if departure.utcoffset() is None:
        raise ValueError("Departure must include a time zone")
    departure = departure.astimezone(UTC)
    matrix = delivery_task["travel_matrix"]
    packages_by_stop = delivery_task["packages_by_stop"]
    details = {}
    elapsed = driving = waiting = planned_service = Decimal(0)
    previous = station

    for stop_id in visits:
        summary = packages_by_stop[stop_id]
        travel = Decimal(str(matrix[previous][stop_id]))
        arrival = elapsed + travel
        intersection = summary["time_window_intersection"]
        service_start = max(arrival, _seconds(intersection[0] - departure)) if intersection else arrival
        wait = service_start - arrival
        # Sum the package values before they are combined into a floating-point subtotal.
        service = sum(
            (Decimal(str(package["planned_service_time_seconds"])) for package in summary["packages"].values()),
            Decimal(0),
        )
        elapsed = service_start + service
        driving += travel
        waiting += wait
        planned_service += service

        violations = {}
        for package_id, (start, end) in summary["package_time_windows"].items():
            if service_start < _seconds(start - departure):
                reason = "before_window"
            elif service_start > _seconds(end - departure):
                reason = "after_window"
            else:
                continue
            violations[package_id] = {"reason": reason, "time_window": (start, end)}

        details[stop_id] = {
            "arrival_datetime_utc": _timestamp(departure, arrival),
            "service_start_datetime_utc": _timestamp(departure, service_start),
            "departure_datetime_utc": _timestamp(departure, elapsed),
            "travel_seconds": float(travel),
            "waiting_seconds": float(wait),
            "planned_service_time_seconds_sum": float(service),
            "has_empty_time_window_intersection": summary["has_empty_time_window_intersection"],
            "window_violations": violations,
        }
        previous = stop_id

    return_leg = Decimal(str(matrix[previous][station])) if visits else Decimal(0)
    return {
        "departure_datetime_utc": departure,
        "return_datetime_utc": _timestamp(departure, elapsed + return_leg),
        "driving_seconds": float(driving + return_leg),
        "return_travel_seconds": float(return_leg),
        "planned_service_time_seconds_sum": float(planned_service),
        "waiting_seconds": float(waiting),
        "total_operation_seconds": float(elapsed + return_leg),
        "risk_package_count": sum(len(stop["window_violations"]) for stop in details.values()),
        "risk_stop_count": sum(bool(stop["window_violations"]) for stop in details.values()),
        "window_conflict_stop_count": sum(stop["has_empty_time_window_intersection"] for stop in details.values()),
        "stop_details": details,
    }
