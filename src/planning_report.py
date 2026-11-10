"""Build JSON and CSV reports from a saved planning snapshot."""

import csv
import json
from datetime import UTC
from io import StringIO


_TIME_FIELDS = ("arrival_datetime_utc", "service_start_datetime_utc", "departure_datetime_utc")
_STOP_FIELDS = ("visit_number", "stop_id", "stop_type", *_TIME_FIELDS)


def snapshot_suggested_stops(order: list[str], stops: dict, schedule: dict | None) -> list[dict]:
    """Capture reliable visit times from the original evaluation, if available."""
    rows = []
    for index, stop_id in enumerate(order):
        times = {}
        if schedule is not None:
            if index == 0:
                times = {"departure_datetime_utc": schedule["departure_datetime_utc"]}
            elif index == len(order) - 1:
                times = {"arrival_datetime_utc": schedule["return_datetime_utc"]}
            else:
                times = schedule["stop_details"][stop_id]

        row = {"visit_number": index + 1, "stop_id": stop_id, "stop_type": stops[stop_id]["type"]}
        for field in _TIME_FIELDS:
            stamp = times.get(field)
            row[field] = stamp.astimezone(UTC).isoformat() if stamp is not None else None
        rows.append(row)
    return rows


def report_stops(run: dict) -> tuple[list[dict], str]:
    """Return the available visit data and identify the provenance of its timings."""
    order = run["suggested_order"]
    if not order:
        return [], "no_suggested_route"
    if (rows := run.get("suggested_stops")) is not None:
        has_timings = any(row.get(field) is not None for row in rows for field in _TIME_FIELDS)
        return rows, "saved_snapshot" if has_timings else "not_saved"

    # Earlier runs saved the single-depot closed order and sometimes the return time.
    stops = {stop_id: {"type": "Station" if stop_id == order[0] else "Dropoff"} for stop_id in order}
    rows = snapshot_suggested_stops(order, stops, None)
    return_time = (run.get("suggested_metrics") or {}).get("return_datetime_utc")
    rows[-1]["arrival_datetime_utc"] = return_time
    return rows, "return_time_only" if return_time is not None else "not_saved"


def export_planning_json(run: dict, review: dict) -> str:
    rows, source = report_stops(run)
    report = {
        **run,
        "record_type": "local_planning_simulation",
        "review": review,
        "stop_timing_source": source,
        "suggested_stops": rows if run["suggested_order"] else None,
    }
    return json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n"


def export_stop_order_csv(run: dict) -> str:
    rows, source = report_stops(run)
    output = StringIO(newline="")
    writer = csv.DictWriter(output, ("run_id", "route_id", *_STOP_FIELDS, "stop_timing_source"))
    writer.writeheader()
    writer.writerows(
        {"run_id": run["run_id"], "route_id": run["route_id"], "stop_timing_source": source, **row}
        for row in rows
    )
    return output.getvalue()
