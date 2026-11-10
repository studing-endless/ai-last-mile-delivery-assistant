"""Load directed travel times and calculate the driving time of a stop order."""

from collections.abc import Iterable, Sequence
from decimal import Decimal
from itertools import chain, pairwise
from math import fsum
from pathlib import Path
from sys import float_info

import ijson


def load_travel_matrix(
    route_id: str, data_dir: Path, stop_ids: Iterable[str]
) -> dict[str, dict[str, float]]:
    """Stream one Amazon route and validate every directed pair of distinct stops."""
    # Amazon route IDs occupy a single segment of the ijson prefix.
    if not route_id or "." in route_id:
        raise ValueError(f"Invalid Amazon route ID: {route_id!r}")

    path = data_dir / "travel_times.json"
    try:
        with path.open("rb") as source:
            matrix = next(ijson.items(source, route_id))
    except StopIteration:
        raise KeyError(f"Route {route_id!r} not found in {path.name}") from None
    except ijson.JSONError as error:
        raise ValueError(f"Could not read route {route_id!r} from {path.name}: {error}") from error

    expected_stops = set(stop_ids)
    if not isinstance(matrix, dict):
        raise ValueError(f"Route {route_id!r} travel matrix must be a mapping")
    if matrix.keys() != expected_stops:
        missing = sorted(expected_stops - matrix.keys())
        extra = sorted(matrix.keys() - expected_stops)
        raise ValueError(f"Route {route_id!r} matrix origins do not match: missing={missing}, extra={extra}")

    for origin, row in matrix.items():
        if not isinstance(row, dict):
            raise ValueError(f"Route {route_id!r} matrix row for {origin!r} must be a mapping")

        # A stop's self-edge is optional, but must be valid when supplied.
        missing = expected_stops - row.keys() - {origin}
        extra = row.keys() - expected_stops
        if missing or extra:
            raise ValueError(
                f"Route {route_id!r} destinations from {origin!r} do not match: "
                f"missing={sorted(missing)}, extra={sorted(extra)}"
            )

        for destination, seconds in row.items():
            if type(seconds) not in (int, Decimal) or not 0 <= seconds <= float_info.max:
                raise ValueError(
                    f"Route {route_id!r} invalid travel time from {origin!r} to {destination!r}: "
                    f"{seconds!r}. Expected a finite non-negative number of seconds."
                )
            row[destination] = float(seconds)

    return matrix


def calculate_driving_seconds(
    ordered_stops: Sequence[str],
    matrix: dict[str, dict[str, float]],
    return_to_station: bool = False,
) -> float:
    """Sum the directed legs of a stop order, optionally closing the trip."""
    legs = pairwise(ordered_stops)
    if return_to_station and ordered_stops and ordered_stops[-1] != ordered_stops[0]:
        legs = chain(legs, ((ordered_stops[-1], ordered_stops[0]),))

    return fsum(matrix[origin][destination] for origin, destination in legs)
