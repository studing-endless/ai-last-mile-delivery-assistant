"""SQLite storage for local planning run snapshots."""

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS planning_runs (
    run_id TEXT PRIMARY KEY NOT NULL,
    route_id TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    solver_status TEXT NOT NULL,
    solve_seconds REAL NOT NULL CHECK (solve_seconds >= 0),
    search_budget_seconds REAL NOT NULL CHECK (search_budget_seconds > 0),
    algorithm TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    suggested_order_json TEXT,
    historical_metrics_json TEXT NOT NULL,
    suggested_metrics_json TEXT,
    error_message TEXT,
    suggested_stops_json TEXT,
    CHECK (suggested_order_json IS NOT NULL OR suggested_metrics_json IS NULL)
);
CREATE INDEX IF NOT EXISTS planning_runs_by_route
    ON planning_runs (route_id, started_at_utc DESC, run_id DESC);
CREATE TABLE IF NOT EXISTS planning_reviews (
    run_id TEXT PRIMARY KEY NOT NULL REFERENCES planning_runs(run_id),
    decision TEXT NOT NULL CHECK (decision IN ('APPROVED', 'REJECTED')),
    reviewed_at_utc TEXT NOT NULL,
    note TEXT NOT NULL CHECK (decision <> 'REJECTED' OR length(trim(note)) > 0)
);
"""


def _has_stop_snapshots(connection: sqlite3.Connection) -> bool:
    return any(column["name"] == "suggested_stops_json" for column in connection.execute("PRAGMA table_info(planning_runs)"))


@contextmanager
def _connect(db_path: Path, *, read_only: bool = False):
    if read_only:
        uri = db_path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            yield connection
        return

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(_SCHEMA)
        if not _has_stop_snapshots(connection):
            connection.execute("BEGIN IMMEDIATE")
            if not _has_stop_snapshots(connection):
                connection.execute("ALTER TABLE planning_runs ADD COLUMN suggested_stops_json TEXT")
            connection.commit()
        yield connection


def _encode(value) -> str:
    return json.dumps(value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def save_run(db_path: Path, run: dict) -> bool:
    """Insert a snapshot. Retrying an identical run is a no-op."""
    if not isfinite(run["solve_seconds"]) or not isfinite(run["search_budget_seconds"]):
        raise ValueError("Solve time and search budget must be finite")

    fields = (
        "run_id", "route_id", "started_at_utc", "solver_status", "solve_seconds",
        "search_budget_seconds", "algorithm", "error_message",
    )
    values = {field: run[field] for field in fields}
    for field in ("parameters", "suggested_order", "historical_metrics", "suggested_metrics"):
        value = run[field]
        values[f"{field}_json"] = _encode(value) if value is not None else None
    stops = run.get("suggested_stops")
    values["suggested_stops_json"] = _encode(stops) if stops is not None else None

    with _connect(db_path) as connection:
        cursor = connection.execute(
            """INSERT INTO planning_runs (
                run_id, route_id, started_at_utc, solver_status, solve_seconds,
                search_budget_seconds, algorithm, error_message, parameters_json,
                suggested_order_json, historical_metrics_json, suggested_metrics_json, suggested_stops_json
            ) VALUES (
                :run_id, :route_id, :started_at_utc, :solver_status, :solve_seconds,
                :search_budget_seconds, :algorithm, :error_message, :parameters_json,
                :suggested_order_json, :historical_metrics_json, :suggested_metrics_json, :suggested_stops_json
            ) ON CONFLICT(run_id) DO NOTHING""",
            values,
        )
        if cursor.rowcount == 1:
            return True
        existing = connection.execute("SELECT * FROM planning_runs WHERE run_id = ?", (run["run_id"],)).fetchone()
        if dict(existing) != values:
            raise ValueError(f"Run ID {run['run_id']!r} already belongs to a different snapshot")
        return False


def list_runs(db_path: Path, route_id: str, *, read_only: bool = False) -> list[dict]:
    """List metadata for the given route, newest first."""
    with _connect(db_path, read_only=read_only) as connection:
        rows = connection.execute(
            """SELECT run_id, route_id, started_at_utc, solver_status, solve_seconds, search_budget_seconds
               FROM planning_runs WHERE route_id = ? ORDER BY started_at_utc DESC, run_id DESC""",
            (route_id,),
        )
        return [dict(row) for row in rows]


def get_run(db_path: Path, run_id: str, *, read_only: bool = False) -> dict | None:
    """Read an individual snapshot without reloading or simulating the route."""
    with _connect(db_path, read_only=read_only) as connection:
        row = connection.execute("SELECT * FROM planning_runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        return None

    run = dict(row)
    for field in ("parameters", "suggested_order", "historical_metrics", "suggested_metrics"):
        value = run.pop(f"{field}_json")
        run[field] = json.loads(value) if value is not None else None
    if (stops := run.pop("suggested_stops_json", None)) is not None:
        run["suggested_stops"] = json.loads(stops)
    return run


def can_approve_run(run: dict) -> bool:
    order = run["suggested_order"]
    return (
        run["solver_status"] in {"ROUTING_SUCCESS", "ROUTING_PARTIAL_SUCCESS_LOCAL_OPTIMUM_NOT_REACHED", "ROUTING_OPTIMAL"}
        and isinstance(order, list)
        and len(order) >= 2
        and all(isinstance(stop, str) and stop for stop in order)
        and order[0] == order[-1]
        and len(set(order[:-1])) == len(order) - 1
    )


def get_review(db_path: Path, run_id: str, *, read_only: bool = False) -> dict | None:
    """Return the final review or a pending state for an existing run."""
    with _connect(db_path, read_only=read_only) as connection:
        row = connection.execute(
            """SELECT p.run_id, COALESCE(r.decision, 'PENDING') AS decision, r.reviewed_at_utc, r.note
               FROM planning_runs p LEFT JOIN planning_reviews r ON p.run_id = r.run_id
               WHERE p.run_id = ?""",
            (run_id,),
        ).fetchone()
        return dict(row) if row is not None else None


def submit_review(db_path: Path, run_id: str, decision: str, note: str = "") -> dict:
    """Record one final human decision. An identical submission returns the existing review."""
    if decision not in ("APPROVED", "REJECTED"):
        raise ValueError("请选择批准或驳回。")
    note = note.strip()
    if decision == "REJECTED" and not note:
        raise ValueError("驳回时必须填写原因。")

    with _connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        run = connection.execute(
            "SELECT solver_status, suggested_order_json FROM planning_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is None:
            raise KeyError(f"找不到规划运行 {run_id!r}。")
        existing = connection.execute("SELECT * FROM planning_reviews WHERE run_id = ?", (run_id,)).fetchone()
        if existing is not None:
            if (existing["decision"], existing["note"]) != (decision, note):
                raise ValueError("该运行已有最终审核决定，无法再次审核。")
            return dict(existing)

        if decision == "APPROVED" and not can_approve_run({
            "solver_status": run["solver_status"],
            "suggested_order": json.loads(run["suggested_order_json"] or "null"),
        }):
            raise ValueError("该运行没有可批准的有效建议路线。")

        timestamp = datetime.now(UTC).isoformat(timespec="microseconds")
        connection.execute(
            "INSERT INTO planning_reviews (run_id, decision, reviewed_at_utc, note) VALUES (?, ?, ?, ?)",
            (run_id, decision, timestamp, note),
        )
        return {"run_id": run_id, "decision": decision, "reviewed_at_utc": timestamp, "note": note}
