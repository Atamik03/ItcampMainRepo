"""
session_store.py
================
Persistent SQLite storage for operator training sessions.

The store keeps an immutable event log that doubles as the training corpus
for the AI error-classification service:

    sessions           -> one row per operator run over a scenario
    actions            -> every operator / automatic action (append-only)
    state_snapshots    -> process context before / after an action or step
    alarms             -> alarm lifecycle (raise / ack / clear)
    error_events       -> rule-based detection + AI classification slots
    expected_actions   -> ground truth reference actions of each scenario
    ai_classifications -> audit of every AI call (labelled dataset)

Storage is event-sourced: events are only inserted (never mutated), so a
session can be fully reconstructed and exported for offline AI analysis.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from models.base import Alarm, ErrorEvent, OperatorAction, SimulationState
from . import db as _db

logger = logging.getLogger("elou_avt.session_store")

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "sessions.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id             TEXT PRIMARY KEY,
    scenario_id    TEXT NOT NULL,
    operator_id    TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'CREATED',
    sim_start      REAL NOT NULL DEFAULT 0.0,
    sim_end        REAL,
    wall_start     REAL NOT NULL,
    wall_end       REAL,
    scheme_version TEXT,
    performance_score REAL,
    qualification  TEXT,
    ai_verdict     TEXT,
    participants   TEXT NOT NULL DEFAULT '[]',
    created_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    seq         INTEGER NOT NULL,
    sim_time    REAL NOT NULL,
    wall_time   REAL,
    operator_id TEXT NOT NULL,
    equipment_id TEXT NOT NULL,
    node_type   TEXT,
    action_type TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    source      TEXT NOT NULL DEFAULT 'operator_panel',
    accepted    INTEGER NOT NULL DEFAULT 1,
    reject_reason TEXT,
    UNIQUE (session_id, seq)
);

CREATE TABLE IF NOT EXISTS state_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT NOT NULL REFERENCES sessions(id),
    seq            INTEGER NOT NULL,
    sim_time       REAL NOT NULL,
    wall_time      REAL,
    reason         TEXT NOT NULL DEFAULT 'step',
    action_id      INTEGER REFERENCES actions(id),
    pressure       TEXT,
    temperature    TEXT,
    levels         TEXT,
    flows          TEXT,
    pump_states    TEXT,
    valve_positions TEXT,
    equipment_states TEXT,
    controller_states TEXT,
    active_alarms  TEXT,
    active_failures TEXT,
    UNIQUE (session_id, seq)
);

CREATE TABLE IF NOT EXISTS alarms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    alarm_id    TEXT NOT NULL,
    parameter   TEXT,
    severity    TEXT,
    actual_value REAL,
    threshold   REAL,
    description TEXT,
    raised_at   REAL NOT NULL,
    acked_at    REAL,
    acked_by    TEXT,
    cleared_at  REAL,
    UNIQUE (session_id, alarm_id, raised_at)
);

CREATE TABLE IF NOT EXISTS error_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT NOT NULL REFERENCES sessions(id),
    sim_time            REAL NOT NULL,
    action_id           INTEGER REFERENCES actions(id),
    rule_error_type     TEXT NOT NULL,
    severity            TEXT,
    expected_action     TEXT,
    cause               TEXT,
    consequence         TEXT,
    context_snapshot_id INTEGER REFERENCES state_snapshots(id),
    ai_class            TEXT,
    ai_confidence       REAL,
    ai_reasoning        TEXT,
    ai_status           TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS expected_actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id TEXT NOT NULL,
    equipment_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    value       TEXT,
    deadline_t  REAL,
    description TEXT,
    consequence TEXT,
    weight      REAL NOT NULL DEFAULT 1.0,
    UNIQUE (scenario_id, equipment_id, action_type)
);

CREATE TABLE IF NOT EXISTS ai_classifications (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT NOT NULL REFERENCES sessions(id),
    error_event_id   INTEGER REFERENCES error_events(id),
    model            TEXT NOT NULL,
    prompt_version   TEXT,
    input_payload    TEXT NOT NULL,
    predicted_class  TEXT NOT NULL,
    confidence       REAL,
    reasoning        TEXT,
    human_correction TEXT,
    human_corrected  INTEGER NOT NULL DEFAULT 0,
    latency_ms       REAL,
    created_at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS session_cause_reviews (
    session_id              TEXT PRIMARY KEY REFERENCES sessions(id),
    feature_schema_version  TEXT NOT NULL,
    features_json           TEXT NOT NULL,
    model_name              TEXT NOT NULL,
    model_status            TEXT NOT NULL,
    predictions_json        TEXT NOT NULL DEFAULT '[]',
    inference_latency_ms    REAL,
    operator_answers_json   TEXT,
    operator_selected_cause TEXT,
    operator_reviewed_at    REAL,
    instructor_id           TEXT,
    instructor_agrees       INTEGER,
    instructor_causes_json  TEXT,
    instructor_reviewed_at  REAL,
    created_at              REAL NOT NULL,
    updated_at              REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_actions_session     ON actions (session_id, seq);
CREATE INDEX IF NOT EXISTS idx_snapshots_session   ON state_snapshots (session_id, sim_time);
CREATE INDEX IF NOT EXISTS idx_alarms_session      ON alarms (session_id, raised_at);
CREATE INDEX IF NOT EXISTS idx_errors_session      ON error_events (session_id, sim_time);
CREATE INDEX IF NOT EXISTS idx_errors_pending      ON error_events (ai_status);
CREATE INDEX IF NOT EXISTS idx_expected_scenario   ON expected_actions (scenario_id);
CREATE INDEX IF NOT EXISTS idx_ai_session          ON ai_classifications (session_id);
CREATE INDEX IF NOT EXISTS idx_cause_review_status ON session_cause_reviews (model_status);

CREATE TABLE IF NOT EXISTS alarm_setpoints (
    parameter    TEXT PRIMARY KEY,
    low_low      REAL,
    low          REAL,
    high         REAL,
    high_high    REAL,
    unit         TEXT,
    updated_at   REAL NOT NULL
);
"""

_SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS sessions (
    id             TEXT PRIMARY KEY,
    scenario_id    TEXT NOT NULL,
    operator_id    TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'CREATED',
    sim_start      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    sim_end        DOUBLE PRECISION,
    wall_start     DOUBLE PRECISION NOT NULL,
    wall_end       DOUBLE PRECISION,
    scheme_version TEXT,
    performance_score DOUBLE PRECISION,
    qualification  TEXT,
    ai_verdict     TEXT,
    participants   TEXT NOT NULL DEFAULT '[]',
    created_at     DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    seq         INTEGER NOT NULL,
    sim_time    DOUBLE PRECISION NOT NULL,
    wall_time   DOUBLE PRECISION,
    operator_id TEXT NOT NULL,
    equipment_id TEXT NOT NULL,
    node_type   TEXT,
    action_type TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    source      TEXT NOT NULL DEFAULT 'operator_panel',
    accepted    INTEGER NOT NULL DEFAULT 1,
    reject_reason TEXT,
    UNIQUE (session_id, seq)
);

CREATE TABLE IF NOT EXISTS state_snapshots (
    id             INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id     TEXT NOT NULL REFERENCES sessions(id),
    seq            INTEGER NOT NULL,
    sim_time       DOUBLE PRECISION NOT NULL,
    wall_time      DOUBLE PRECISION,
    reason         TEXT NOT NULL DEFAULT 'step',
    action_id      INTEGER REFERENCES actions(id),
    pressure       TEXT,
    temperature    TEXT,
    levels         TEXT,
    flows          TEXT,
    pump_states    TEXT,
    valve_positions TEXT,
    equipment_states TEXT,
    controller_states TEXT,
    active_alarms  TEXT,
    active_failures TEXT,
    UNIQUE (session_id, seq)
);

CREATE TABLE IF NOT EXISTS alarms (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    alarm_id    TEXT NOT NULL,
    parameter   TEXT,
    severity    TEXT,
    actual_value DOUBLE PRECISION,
    threshold   DOUBLE PRECISION,
    description TEXT,
    raised_at   DOUBLE PRECISION NOT NULL,
    acked_at    DOUBLE PRECISION,
    acked_by    TEXT,
    cleared_at  DOUBLE PRECISION,
    UNIQUE (session_id, alarm_id, raised_at)
);

CREATE TABLE IF NOT EXISTS error_events (
    id                  INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id          TEXT NOT NULL REFERENCES sessions(id),
    sim_time            DOUBLE PRECISION NOT NULL,
    action_id           INTEGER REFERENCES actions(id),
    rule_error_type     TEXT NOT NULL,
    severity            TEXT,
    expected_action     TEXT,
    cause               TEXT,
    consequence         TEXT,
    context_snapshot_id INTEGER REFERENCES state_snapshots(id),
    ai_class            TEXT,
    ai_confidence       DOUBLE PRECISION,
    ai_reasoning        TEXT,
    ai_status           TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS expected_actions (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scenario_id TEXT NOT NULL,
    equipment_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    value       TEXT,
    deadline_t  DOUBLE PRECISION,
    description TEXT,
    consequence TEXT,
    weight      DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    UNIQUE (scenario_id, equipment_id, action_type)
);

CREATE TABLE IF NOT EXISTS ai_classifications (
    id               INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id       TEXT NOT NULL REFERENCES sessions(id),
    error_event_id   INTEGER REFERENCES error_events(id),
    model            TEXT NOT NULL,
    prompt_version   TEXT,
    input_payload    TEXT NOT NULL,
    predicted_class  TEXT NOT NULL,
    confidence       DOUBLE PRECISION,
    reasoning        TEXT,
    human_correction TEXT,
    human_corrected  INTEGER NOT NULL DEFAULT 0,
    latency_ms       DOUBLE PRECISION,
    created_at       DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS session_cause_reviews (
    session_id              TEXT PRIMARY KEY REFERENCES sessions(id),
    feature_schema_version  TEXT NOT NULL,
    features_json           TEXT NOT NULL,
    model_name              TEXT NOT NULL,
    model_status            TEXT NOT NULL,
    predictions_json        TEXT NOT NULL DEFAULT '[]',
    inference_latency_ms    DOUBLE PRECISION,
    operator_answers_json   TEXT,
    operator_selected_cause TEXT,
    operator_reviewed_at    DOUBLE PRECISION,
    instructor_id           TEXT,
    instructor_agrees       INTEGER,
    instructor_causes_json  TEXT,
    instructor_reviewed_at  DOUBLE PRECISION,
    created_at              DOUBLE PRECISION NOT NULL,
    updated_at              DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_actions_session     ON actions (session_id, seq);
CREATE INDEX IF NOT EXISTS idx_snapshots_session   ON state_snapshots (session_id, sim_time);
CREATE INDEX IF NOT EXISTS idx_alarms_session      ON alarms (session_id, raised_at);
CREATE INDEX IF NOT EXISTS idx_errors_session      ON error_events (session_id, sim_time);
CREATE INDEX IF NOT EXISTS idx_errors_pending      ON error_events (ai_status);
CREATE INDEX IF NOT EXISTS idx_expected_scenario   ON expected_actions (scenario_id);
CREATE INDEX IF NOT EXISTS idx_ai_session          ON ai_classifications (session_id);
CREATE INDEX IF NOT EXISTS idx_cause_review_status ON session_cause_reviews (model_status);

CREATE TABLE IF NOT EXISTS alarm_setpoints (
    parameter    TEXT PRIMARY KEY,
    low_low      DOUBLE PRECISION,
    low          DOUBLE PRECISION,
    high         DOUBLE PRECISION,
    high_high    DOUBLE PRECISION,
    unit         TEXT,
    updated_at   DOUBLE PRECISION NOT NULL
);
"""

_ACTION_TABLES = ("actions", "state_snapshots")


# Shared with lms/store.py and lms/content_store.py -- see persistence/db.py.
_json = _db.json_dump
_unjson = _db.json_load


_JSON_COLUMNS = {
    "sessions": {"ai_verdict", "participants"},
    "actions": {"old_value", "new_value"},
    "state_snapshots": {
        "pressure", "temperature", "levels", "flows", "pump_states",
        "valve_positions", "equipment_states", "controller_states",
        "active_alarms", "active_failures",
    },
    "alarms": set(),
    "error_events": set(),
    "expected_actions": {"value"},
    "ai_classifications": {"input_payload"},
    "session_cause_reviews": {
        "features_json", "predictions_json", "operator_answers_json",
        "instructor_causes_json",
    },
}


def _decode(row: Dict[str, Any], table: str) -> Dict[str, Any]:
    """Deserialize JSON columns of a row back into Python objects."""
    out = dict(row)
    for col in _JSON_COLUMNS[table]:
        out[col] = _unjson(out.get(col))
    return out


class SessionStore:
    """SQLite-backed store for operator training sessions.

    Not thread-bound (a store may be shared across FastAPI worker threads);
    all mutations are serialized with an internal lock. Methods take an
    explicit session_id so a single store can accumulate a training corpus.
    """

    def __init__(self, path: Optional[Union[Path, str]] = None, create_dir: bool = True):
        self._path = Path(path) if path else DEFAULT_DB_PATH
        if create_dir:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = _db.connect(path, DEFAULT_DB_PATH)
        with self._lock:
            self._conn.executescript(_SCHEMA if self._conn.dialect == "sqlite" else _SCHEMA_POSTGRES)
            self._migrate()
            self._conn.commit()
        logger.info("SessionStore opened: %s (%s)", self._path, self._conn.dialect)

    def _migrate(self) -> None:
        """Add missing columns to existing tables (SQLite only; Postgres gets
        the current schema from _SCHEMA_POSTGRES on first create)."""
        if self._conn.dialect != "sqlite":
            return
        cols = [r["name"] for r in self._conn.execute("PRAGMA table_info(sessions)").fetchall()]
        if "participants" not in cols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN participants TEXT NOT NULL DEFAULT '[]'"
            )

    @classmethod
    def in_memory(cls) -> "SessionStore":
        return cls(path=":memory:")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "SessionStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _next_seq(self, session_id: str, table: str) -> int:
        # `table` is only ever called with the two hardcoded literal
        # strings below ("actions", "state_snapshots") -- never with
        # external/user-supplied input.
        row = self._conn.execute(
            f"SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM {table} WHERE session_id = ?",  # nosec B608
            (session_id,),
        ).fetchone()
        return int(row["next_seq"])

    def _touch(self, sql: str, params: tuple) -> None:
        with self._lock, self._conn:
            self._conn.execute(sql, params)

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def begin_session(
        self,
        scenario_id: str,
        operator_id: str,
        session_id: Optional[str] = None,
        scheme_version: str = "",
        sim_start: float = 0.0,
        wall_time: Optional[float] = None,
    ) -> str:
        """Create a new session in CREATED state and return its id."""
        sid = session_id or uuid.uuid4().hex
        now = wall_time if wall_time is not None else time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO sessions (id, scenario_id, operator_id, status, "
                "sim_start, wall_start, scheme_version, created_at) "
                "VALUES (?, ?, ?, 'CREATED', ?, ?, ?, ?)",
                (sid, scenario_id, operator_id, sim_start, now, scheme_version, now),
            )
        logger.info("Session started: %s scenario=%s operator=%s", sid, scenario_id, operator_id)
        return sid

    def start_session(self, session_id: str, sim_start: float = 0.0, wall_time: Optional[float] = None) -> None:
        """Move a CREATED session to RUNNING."""
        self._touch(
            "UPDATE sessions SET status='RUNNING', sim_start=?, wall_start=? WHERE id=?",
            (sim_start, wall_time if wall_time is not None else time.time(), session_id),
        )

    def finish_session(
        self,
        session_id: str,
        sim_end: float,
        score: Optional[float] = None,
        qualification: str = "",
        ai_verdict: Optional[Dict[str, Any]] = None,
        wall_time: Optional[float] = None,
    ) -> None:
        """Complete the session with score, grade and optional AI verdict."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE sessions SET status='COMPLETED', sim_end=?, wall_end=?, "
                "performance_score=?, qualification=?, ai_verdict=? WHERE id=?",
                (
                    sim_end,
                    wall_time if wall_time is not None else time.time(),
                    score,
                    qualification,
                    _json(ai_verdict),
                    session_id,
                ),
            )

    def abort_session(self, session_id: str, reason: str = "", wall_time: Optional[float] = None) -> None:
        self._touch(
            "UPDATE sessions SET status='ABORTED', wall_end=? WHERE id=?",
            (wall_time if wall_time is not None else time.time(), session_id),
        )

    def set_ai_verdict(self, session_id: str, verdict: Dict[str, Any]) -> None:
        self._touch(
            "UPDATE sessions SET ai_verdict=? WHERE id=?",
            (_json(verdict), session_id),
        )

    def set_participants(self, session_id: str, usernames: List[str]) -> None:
        """Store the list of participant usernames for a practice session."""
        clean = []
        for u in usernames or []:
            u = (u or "").strip()
            if u and u not in clean:
                clean.append(u)
        self._touch(
            "UPDATE sessions SET participants=? WHERE id=?",
            (_json(clean), session_id),
        )

    def set_operator(self, session_id: str, operator_id: str) -> None:
        """Привязать реального оператора к сессии (мультиоператорная гейтка)."""
        self._touch(
            "UPDATE sessions SET operator_id=? WHERE id=?",
            (operator_id, session_id),
        )

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return _decode(dict(row), "sessions") if row else None

    def list_sessions(self, scenario_id: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM sessions"
        params: tuple = ()
        if scenario_id:
            sql += " WHERE scenario_id = ?"
            params = (scenario_id,)
        sql += " ORDER BY created_at DESC LIMIT ?"
        rows = self._conn.execute(sql, params + (limit,)).fetchall()
        return [_decode(dict(r), "sessions") for r in rows]

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def append_action(
        self,
        session_id: str,
        action: OperatorAction,
        wall_time: Optional[float] = None,
        accepted: bool = True,
        reject_reason: str = "",
        node_type: Optional[str] = None,
    ) -> int:
        """Append one operator action to the session log. Returns row id."""
        with self._lock, self._conn:
            seq = self._next_seq(session_id, "actions")
            row_id = self._conn.insert_returning_id(
                "INSERT INTO actions (session_id, seq, sim_time, wall_time, operator_id, "
                "equipment_id, node_type, action_type, old_value, new_value, source, "
                "accepted, reject_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    seq,
                    action.timestamp,
                    wall_time if wall_time is not None else time.time(),
                    action.operator_id,
                    action.equipment_id,
                    node_type,
                    action.action_type.value if hasattr(action.action_type, "value") else action.action_type,
                    _json(action.old_value),
                    _json(action.new_value),
                    action.source,
                    1 if accepted else 0,
                    reject_reason,
                ),
            )
        logger.debug("Action logged: %s on %s", action.action_type, action.equipment_id)
        return row_id

    def get_actions(self, session_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM actions WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        return [_decode(dict(r), "actions") for r in rows]

    # ------------------------------------------------------------------
    # State snapshots
    # ------------------------------------------------------------------

    def append_snapshot(
        self,
        session_id: str,
        state: SimulationState,
        reason: str = "step",
        action_id: Optional[int] = None,
        sim_time: Optional[float] = None,
        wall_time: Optional[float] = None,
    ) -> int:
        """Persist a process context snapshot. Returns row id."""
        t = sim_time if sim_time is not None else state.timestamp
        with self._lock, self._conn:
            seq = self._next_seq(session_id, "state_snapshots")
            row_id = self._conn.insert_returning_id(
                "INSERT INTO state_snapshots (session_id, seq, sim_time, wall_time, reason, "
                "action_id, pressure, temperature, levels, flows, pump_states, valve_positions, "
                "equipment_states, controller_states, active_alarms, active_failures) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    seq,
                    t,
                    wall_time if wall_time is not None else time.time(),
                    reason,
                    action_id,
                    _json(state.pressure),
                    _json(state.temperature),
                    _json(state.level),
                    _json({"feed_flow": state.feed_flow, "product_flow": state.product_flow}),
                    _json(state.pump_states),
                    _json(state.valve_positions),
                    _json(state.equipment_states),
                    _json(state.controllers),
                    _json([a.model_dump() for a in state.alarms]),
                    _json(state.active_failures),
                ),
            )
        return row_id

    def get_snapshots(self, session_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM state_snapshots WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        return [_decode(dict(r), "state_snapshots") for r in rows]

    def get_first_snapshot(self, session_id: str, reason: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Return one start snapshot without deserializing the whole time series."""
        sql = "SELECT * FROM state_snapshots WHERE session_id = ?"
        params: tuple = (session_id,)
        if reason is not None:
            sql += " AND reason = ?"
            params += (reason,)
        sql += " ORDER BY seq LIMIT 1"
        row = self._conn.execute(sql, params).fetchone()
        return _decode(dict(row), "state_snapshots") if row else None

    # ------------------------------------------------------------------
    # Alarms
    # ------------------------------------------------------------------

    def append_alarm(self, session_id: str, alarm: Alarm) -> int:
        """Record an alarm raise. Returns row id (or existing id on replay)."""
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT id FROM alarms WHERE session_id=? AND alarm_id=? AND raised_at=?",
                (session_id, alarm.id, alarm.timestamp),
            ).fetchone()
            if existing:
                return int(existing["id"])
            return self._conn.insert_returning_id(
                "INSERT INTO alarms (session_id, alarm_id, parameter, severity, actual_value, "
                "threshold, description, raised_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    alarm.id,
                    alarm.parameter,
                    alarm.severity.value if hasattr(alarm.severity, "value") else alarm.severity,
                    alarm.actual_value,
                    alarm.threshold,
                    alarm.description,
                    alarm.timestamp,
                ),
            )

    def update_alarm_event(
        self,
        session_id: str,
        alarm_id: str,
        acked_at: Optional[float] = None,
        acked_by: Optional[str] = None,
        cleared_at: Optional[float] = None,
    ) -> None:
        fields: List[str] = []
        params: List[Any] = []
        if acked_at is not None:
            fields.append("acked_at=?")
            params.append(acked_at)
        if acked_by is not None:
            fields.append("acked_by=?")
            params.append(acked_by)
        if cleared_at is not None:
            fields.append("cleared_at=?")
            params.append(cleared_at)
        if not fields:
            return
        params.extend((session_id, alarm_id))
        # `fields` entries are the three hardcoded literal strings above
        # ("acked_at=?", "acked_by=?", "cleared_at=?") -- never derived
        # from external/user-supplied column names.
        self._touch(
            f"UPDATE alarms SET {', '.join(fields)} "  # nosec B608
            "WHERE session_id=? AND alarm_id=? AND cleared_at IS NULL",
            tuple(params),
        )

    def get_alarms(self, session_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM alarms WHERE session_id = ? ORDER BY raised_at",
            (session_id,),
        ).fetchall()
        return [_decode(dict(r), "alarms") for r in rows]

    # ------------------------------------------------------------------
    # Error events + AI classification
    # ------------------------------------------------------------------

    def append_error(
        self,
        session_id: str,
        error: ErrorEvent,
        action_id: Optional[int] = None,
        context_snapshot_id: Optional[int] = None,
        ai_status: str = "pending",
    ) -> int:
        """Record a rule-detected operator error. Returns row id."""
        with self._lock, self._conn:
            return self._conn.insert_returning_id(
                "INSERT INTO error_events (session_id, sim_time, action_id, rule_error_type, "
                "severity, expected_action, cause, consequence, context_snapshot_id, ai_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    error.timestamp,
                    action_id,
                    error.error_type,
                    error.severity.value if hasattr(error.severity, "value") else error.severity,
                    error.expected_action,
                    error.cause,
                    error.consequence,
                    context_snapshot_id,
                    ai_status,
                ),
            )

    def get_errors(self, session_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM error_events WHERE session_id = ? ORDER BY sim_time",
            (session_id,),
        ).fetchall()
        return [_decode(dict(r), "error_events") for r in rows]

    def link_error_action(self, error_event_id: int, action_id: int) -> None:
        """Link an error event to the operator action that triggered it."""
        self._touch(
            "UPDATE error_events SET action_id=? WHERE id=?",
            (action_id, error_event_id),
        )

    def get_unclassified_errors(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return error events still awaiting AI classification (training queue)."""
        rows = self._conn.execute(
            "SELECT * FROM error_events WHERE ai_status = 'pending' ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
        return [_decode(dict(r), "error_events") for r in rows]

    def set_error_classification(
        self,
        error_event_id: int,
        ai_class: str,
        confidence: Optional[float] = None,
        reasoning: str = "",
    ) -> None:
        self._touch(
            "UPDATE error_events SET ai_class=?, ai_confidence=?, ai_reasoning=?, "
            "ai_status='classified' WHERE id=?",
            (ai_class, confidence, reasoning, error_event_id),
        )

    def append_ai_classification(
        self,
        session_id: str,
        predicted_class: str,
        input_payload: Dict[str, Any],
        model: str = "rule_based_v0",
        prompt_version: str = "",
        error_event_id: Optional[int] = None,
        confidence: Optional[float] = None,
        reasoning: str = "",
        latency_ms: Optional[float] = None,
    ) -> int:
        """Audit one AI classification call. This row is the labelled dataset."""
        with self._lock, self._conn:
            return self._conn.insert_returning_id(
                "INSERT INTO ai_classifications (session_id, error_event_id, model, prompt_version, "
                "input_payload, predicted_class, confidence, reasoning, latency_ms, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    error_event_id,
                    model,
                    prompt_version,
                    _json(input_payload),
                    predicted_class,
                    confidence,
                    reasoning,
                    latency_ms,
                    time.time(),
                ),
            )

    def get_classifications(self, session_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM ai_classifications WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        return [_decode(dict(r), "ai_classifications") for r in rows]

    def correct_classification(self, classification_id: int, correction: str) -> None:
        self._touch(
            "UPDATE ai_classifications SET human_correction=?, human_corrected=1 WHERE id=?",
            (correction, classification_id),
        )

    # ------------------------------------------------------------------
    # Session-level ML predictions and human review
    # ------------------------------------------------------------------

    def save_cause_prediction(
        self, session_id: str, features: Dict[str, Any], predictions: List[Dict[str, Any]],
        model_name: str, model_status: str, latency_ms: Optional[float] = None,
        feature_schema_version: str = "error-cause-34-v1",
    ) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO session_cause_reviews (session_id, feature_schema_version, "
                "features_json, model_name, model_status, predictions_json, "
                "inference_latency_ms, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET feature_schema_version=excluded.feature_schema_version, "
                "features_json=excluded.features_json, model_name=excluded.model_name, "
                "model_status=excluded.model_status, predictions_json=excluded.predictions_json, "
                "inference_latency_ms=excluded.inference_latency_ms, updated_at=excluded.updated_at",
                (session_id, feature_schema_version, _json(features), model_name, model_status,
                 _json(predictions), latency_ms, now, now),
            )

    def get_cause_review(self, session_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM session_cause_reviews WHERE session_id=?", (session_id,)
        ).fetchone()
        return _decode(dict(row), "session_cause_reviews") if row else None

    def save_operator_cause_review(
        self, session_id: str, answers: Dict[str, bool], selected_cause: str = "",
    ) -> None:
        self._touch(
            "UPDATE session_cause_reviews SET operator_answers_json=?, "
            "operator_selected_cause=?, operator_reviewed_at=?, updated_at=? WHERE session_id=?",
            (_json(answers), selected_cause or None, time.time(), time.time(), session_id),
        )

    def save_instructor_cause_review(
        self, session_id: str, instructor_id: str, agrees: bool,
        causes: Optional[List[str]] = None,
    ) -> None:
        self._touch(
            "UPDATE session_cause_reviews SET instructor_id=?, instructor_agrees=?, "
            "instructor_causes_json=?, instructor_reviewed_at=?, updated_at=? WHERE session_id=?",
            (instructor_id, 1 if agrees else 0, _json(causes or []),
             time.time(), time.time(), session_id),
        )

    # ------------------------------------------------------------------
    # Expected (reference) actions
    # ------------------------------------------------------------------

    def seed_expected_actions(self, scenario_id: str, reference_actions: List[Dict[str, Any]]) -> int:
        """Insert ground-truth reference actions for a scenario (idempotent)."""
        count = 0
        with self._lock, self._conn:
            insert_sql = (
                f"{self._conn.ignore_prefix()} expected_actions (scenario_id, equipment_id, action_type, "
                "value, deadline_t, description, consequence, weight) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                f"{self._conn.ignore_suffix('scenario_id, equipment_id, action_type')}"
            )
            for ra in reference_actions:
                cur = self._conn.execute(
                    insert_sql,
                    (
                        scenario_id,
                        ra.get("equipment", ra.get("equipment_id", ra.get("object_id", ""))),
                        ra.get("action", ra.get("action_type", "")),
                        _json(ra.get("value")),
                        ra.get("t", ra.get("deadline_t")),
                        ra.get("description", ""),
                        ra.get("consequence", ""),
                        ra.get("weight", 1.0),
                    ),
                )
                count += cur.rowcount
        return count

    def get_expected_actions(self, scenario_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM expected_actions WHERE scenario_id = ? ORDER BY deadline_t",
            (scenario_id,),
        ).fetchall()
        return [_decode(dict(r), "expected_actions") for r in rows]

    # ------------------------------------------------------------------
    # Alarm setpoints (manual operator overrides, survive restarts)
    # ------------------------------------------------------------------

    def save_alarm_setpoint(
        self,
        parameter: str,
        low_low: Optional[float],
        low: Optional[float],
        high: Optional[float],
        high_high: Optional[float],
        unit: str,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO alarm_setpoints (parameter, low_low, low, high, high_high, unit, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(parameter) DO UPDATE SET "
                "low_low=excluded.low_low, low=excluded.low, high=excluded.high, "
                "high_high=excluded.high_high, unit=excluded.unit, updated_at=excluded.updated_at",
                (parameter, low_low, low, high, high_high, unit, time.time()),
            )

    def clear_alarm_setpoints(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM alarm_setpoints")

    def load_alarm_setpoints(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT parameter, low_low, low, high, high_high, unit FROM alarm_setpoints ORDER BY parameter"
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Dump the full session as one JSON-serializable dict (AI dataset row)."""
        session = self.get_session(session_id)
        if session is None:
            return None
        return {
            "session": session,
            "actions": self.get_actions(session_id),
            "snapshots": self.get_snapshots(session_id),
            "alarms": self.get_alarms(session_id),
            "error_events": self.get_errors(session_id),
            "ai_classifications": self.get_classifications(session_id),
        }
