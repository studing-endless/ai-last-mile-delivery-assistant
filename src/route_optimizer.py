"""Plan a single closed route using directed driving times."""

from math import isfinite
from time import perf_counter

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from .travel_time_loader import calculate_driving_seconds


def _millisecond_costs(stop_ids: list[str], matrix: dict) -> list[list[int]]:
    """Round each directed leg to the nearest millisecond, with ties to even."""
    max_cost = ((1 << 63) - 1) // len(stop_ids)
    costs = []
    for origin in stop_ids:
        row = []
        for destination in stop_ids:
            if origin == destination:
                # A same-node solver transition represents no driving.
                row.append(0)
                continue
            milliseconds = matrix[origin][destination] * 1000
            if not 0 <= milliseconds <= max_cost:
                raise ValueError(f"Travel time from {origin!r} to {destination!r} exceeds the solver cost range")
            row.append(round(milliseconds))
        costs.append(row)
    return costs


def optimize_route(delivery_task: dict, time_limit_seconds: float = 20) -> dict:
    """Search for a closed tour and compare its driving time with the historical tour."""
    budget_ms = time_limit_seconds * 1000
    if not isfinite(budget_ms) or budget_ms < 1:
        raise ValueError("The search budget must be finite and at least one millisecond")

    station = delivery_task["station_id"]
    dropoffs = [stop_id for stop_id, stop in delivery_task["stops"].items() if stop["type"] == "Dropoff"]
    stop_ids = [station, *dropoffs]
    matrix = delivery_task["travel_matrix"]

    manager = pywrapcp.RoutingIndexManager(len(stop_ids), 1, 0)
    routing = pywrapcp.RoutingModel(manager)
    transit = routing.RegisterTransitMatrix(_millisecond_costs(stop_ids, matrix))
    routing.SetArcCostEvaluatorOfAllVehicles(transit)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromMilliseconds(round(budget_ms))

    started = perf_counter()
    solution = routing.SolveWithParameters(params)
    elapsed = perf_counter() - started

    historical_seconds = calculate_driving_seconds(delivery_task["ordered_stops"], matrix, return_to_station=True)
    result = {
        "status": routing_enums_pb2.RoutingSearchStatus.Value.Name(routing.status()),
        "found_solution": solution is not None,
        "suggested_order": None,
        "suggested_driving_seconds": None,
        "historical_driving_seconds": historical_seconds,
        "saved_seconds": None,
        "saved_percent": None,
        "solve_seconds": elapsed,
    }
    if solution is None:
        return result

    order = []
    index = routing.Start(0)
    while not routing.IsEnd(index):
        order.append(stop_ids[manager.IndexToNode(index)])
        index = solution.Value(routing.NextVar(index))
    order.append(stop_ids[manager.IndexToNode(index)])

    visits = order[1:-1]
    if order[0] != station or order[-1] != station or len(visits) != len(dropoffs) or set(visits) != set(dropoffs):
        raise RuntimeError("Solver returned an invalid route for the given station and dropoffs")

    suggested_seconds = calculate_driving_seconds(order[:-1], matrix, return_to_station=True)
    saved = historical_seconds - suggested_seconds
    result.update(
        suggested_order=order,
        suggested_driving_seconds=suggested_seconds,
        saved_seconds=saved,
        saved_percent=100 * (saved / historical_seconds) if historical_seconds else None,
    )
    return result
