"""Collect every session from sessions.db into a pandas DataFrame.

Run without arguments: ``python ml/collect_session_dataset.py``.
The script only reads SQLite and writes ``ml/session_dataset.csv``.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any, Mapping, Sequence

import pandas as pd


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "sessions.db"
DEFAULT_OUTPUT_PATH = Path(__file__).resolve().parent / "session_dataset.csv"

ERROR_TYPES = (
    "WRONG_SEQUENCE",
    "DELAYED_ACTION",
    "WRONG_EQUIPMENT",
    "WRONG_ACTION_TYPE",
    "MISSED_ACTION",
    "EXTRA_ACTION",
)


def _json_load(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _rows(conn: sqlite3.Connection, table: str, session_id: str, order: str) -> list[dict[str, Any]]:
    # Every call site below passes hardcoded literal strings for `table`
    # and `order` (this is an offline maintainer CLI, not reachable from
    # any live API); `session_id`, the only externally-supplied value,
    # is properly bound as a real ? parameter.
    if not _table_exists(conn, table) or "session_id" not in _columns(conn, table):
        return []
    query = f'SELECT * FROM "{table}" WHERE session_id = ? ORDER BY {order}'  # nosec B608
    return [dict(row) for row in conn.execute(query, (session_id,)).fetchall()]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _interval_features(prefix: str, times: Sequence[float]) -> dict[str, float]:
    gaps = [right - left for left, right in zip(times, times[1:]) if right >= left]
    return {
        f"{prefix}_mean_interval_s": mean(gaps) if gaps else 0.0,
        f"{prefix}_max_interval_s": max(gaps, default=0.0),
    }


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_").lower() or "unknown"


def _snapshot_features(snapshots: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {"snapshot_count": len(snapshots)}
    groups = ("pressure", "temperature", "levels", "flows")
    observed: dict[tuple[str, str], list[float]] = {}
    for snapshot in snapshots:
        for group in groups:
            values = _json_load(snapshot.get(group), {})
            if not isinstance(values, Mapping):
                continue
            for key, raw in values.items():
                value = _number(raw)
                if value is not None:
                    observed.setdefault((group, str(key)), []).append(value)
    for (group, key), values in observed.items():
        prefix = f"state_{group}_{_safe_name(key)}"
        result.update({
            f"{prefix}_start": values[0],
            f"{prefix}_end": values[-1],
            f"{prefix}_min": min(values),
            f"{prefix}_max": max(values),
            f"{prefix}_delta": values[-1] - values[0],
        })
    return result


def _build_row(
    conn: sqlite3.Connection,
    session: Mapping[str, Any],
) -> dict[str, Any]:
    session_id = str(session["id"])
    scenario_id = str(session["scenario_id"])
    actions = _rows(conn, "actions", session_id, "seq")
    snapshots = _rows(conn, "state_snapshots", session_id, "seq")
    alarms = _rows(conn, "alarms", session_id, "raised_at")
    errors = _rows(conn, "error_events", session_id, "sim_time")
    ui_events = _rows(conn, "lms_scada_log", session_id, "timestamp")
    expected = []
    if _table_exists(conn, "expected_actions"):
        expected = [
            dict(row) for row in conn.execute(
                "SELECT * FROM expected_actions WHERE scenario_id=? ORDER BY deadline_t, id",
                (scenario_id,),
            ).fetchall()
        ]

    sim_start = _number(session.get("sim_start")) or 0.0
    sim_end = _number(session.get("sim_end"))
    wall_start = _number(session.get("wall_start"))
    wall_end = _number(session.get("wall_end"))
    action_times = [float(a["sim_time"]) for a in actions if _number(a.get("sim_time")) is not None]
    error_times = [float(e["sim_time"]) for e in errors if _number(e.get("sim_time")) is not None]
    error_counts = Counter(str(e.get("rule_error_type", "UNKNOWN")) for e in errors)
    severity_counts = Counter(str(e.get("severity", "UNKNOWN")) for e in errors)
    alarm_severity = Counter(str(a.get("severity", "UNKNOWN")) for a in alarms)
    accepted_count = sum(int(a.get("accepted", 1) or 0) for a in actions)
    equipment = {str(a.get("equipment_id")) for a in actions if a.get("equipment_id")}
    action_pairs = {(a.get("equipment_id"), a.get("action_type")) for a in actions}
    expected_pairs = {(a.get("equipment_id"), a.get("action_type")) for a in expected}
    row: dict[str, Any] = {
        "session_id": session_id,
        "scenario_id": scenario_id,
        "scheme_version": session.get("scheme_version"),
        "session_status": session.get("status"),
        "session_duration_sim_s": max(0.0, (sim_end or sim_start) - sim_start),
        "session_duration_wall_s": max(0.0, (wall_end or wall_start or 0.0) - (wall_start or 0.0)),
        "data_has_sim_end": int(sim_end is not None),
        "data_has_snapshots": int(bool(snapshots)),
        "data_has_ui_events": int(bool(ui_events)),
        "action_count": len(actions),
        "action_accepted_count": accepted_count,
        "action_rejected_count": len(actions) - accepted_count,
        "action_acceptance_ratio": accepted_count / len(actions) if actions else 0.0,
        "action_unique_equipment_count": len(equipment),
        "action_extra_pair_count": len(action_pairs - expected_pairs),
        "expected_action_count": len(expected),
        "expected_pair_coverage": len(action_pairs & expected_pairs) / len(expected_pairs) if expected_pairs else 0.0,
        "error_count": len(errors),
        "error_unique_type_count": len(error_counts),
        "error_first_at_s": max(0.0, error_times[0] - sim_start) if error_times else None,
        "error_last_at_s": max(0.0, error_times[-1] - sim_start) if error_times else None,
        "alarm_count": len(alarms),
        "alarm_unacked_count": sum(a.get("acked_at") is None for a in alarms),
        "alarm_uncleared_count": sum(a.get("cleared_at") is None for a in alarms),
        "alarm_mean_ack_delay_s": mean([
            float(a["acked_at"]) - float(a["raised_at"])
            for a in alarms if _number(a.get("acked_at")) is not None
        ]) if any(_number(a.get("acked_at")) is not None for a in alarms) else None,
        "ui_event_count": len(ui_events),
        "ui_unique_object_count": len({e.get("object_id") for e in ui_events if e.get("object_id")}),
        "ui_total_duration_s": sum(_number(e.get("duration_s")) or 0.0 for e in ui_events),
        "actions_json": _json_dump(actions),
        "expected_actions_json": _json_dump(expected),
        "errors_json": _json_dump(errors),
        "alarms_json": _json_dump(alarms),
        "snapshots_json": _json_dump(snapshots),
        "ui_events_json": _json_dump(ui_events),
    }
    row.update(_interval_features("action", action_times))
    row.update(_interval_features("error", error_times))
    row.update(_snapshot_features(snapshots))
    for error_type in ERROR_TYPES:
        row[f"error_{error_type.lower()}_count"] = error_counts[error_type]
    for severity in ("LOW", "MEDIUM", "HIGH", "CRITICAL"):
        row[f"error_severity_{severity.lower()}_count"] = severity_counts[severity]
        row[f"alarm_severity_{severity.lower()}_count"] = alarm_severity[severity]
    return row


def build_session_dataframe(
    db_path: str | Path = DEFAULT_DB_PATH,
) -> pd.DataFrame:
    """Return a DataFrame with one row for every session in the database."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        if not _table_exists(conn, "sessions"):
            raise ValueError("В базе данных отсутствует таблица sessions")
        sessions = conn.execute("SELECT * FROM sessions ORDER BY created_at").fetchall()
        return pd.DataFrame(
            [_build_row(conn, dict(session)) for session in sessions]
        ).convert_dtypes()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args()
    frame = build_session_dataframe(args.db)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.suffix.lower() == ".parquet":
        frame.to_parquet(args.output, index=False)
    else:
        frame.to_csv(args.output, index=False)
    print(f"Сохранено сессий: {len(frame)}; столбцов: {len(frame.columns)}; файл: {args.output}")


if __name__ == "__main__":
    main()
