"""
lms/store.py
============
SQLite storage for the LMS layer. Uses the same `sessions.db` file as
`auth.store` and `persistence.session_store`, with the same conventions
(WAL, busy_timeout, foreign_keys, RLock).

Tables:
    lms_groups, lms_group_members       study groups + operators
    lms_courses, lms_course_modules     training programs
    lms_competencies, lms_user_competencies  skill catalogue + levels
    lms_practice_tasks                  task library (bound to scenarios)
    lms_user_progress                   per-user module progress
    lms_notifications                   user feed
    lms_settings                        key/value system settings
    lms_system_log                      administrator journal
    lms_chat_messages                   practice chat (multi-operator)
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from persistence import db as _db

from .models import (
    Competency,
    GroupCreate,
    GroupUpdate,
    ModuleCreate,
    ModuleStatus,
    TaskCreate,
    TaskUpdate,
    UserProgress,
)

logger = logging.getLogger("elou_avt.lms")

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "sessions.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lms_groups (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    course_id    INTEGER,
    instructor_id INTEGER,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS lms_group_members (
    group_id INTEGER NOT NULL REFERENCES lms_groups(id) ON DELETE CASCADE,
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, user_id)
);

CREATE TABLE IF NOT EXISTS lms_courses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'DRAFT',
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS lms_course_modules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id   INTEGER NOT NULL REFERENCES lms_courses(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    seq         INTEGER NOT NULL DEFAULT 0,
    content     TEXT NOT NULL DEFAULT '',
    scenario_id TEXT,
    practice_task_id INTEGER
);

CREATE TABLE IF NOT EXISTS lms_competencies (
    code        TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS lms_user_competencies (
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    competency_code TEXT NOT NULL REFERENCES lms_competencies(code) ON DELETE CASCADE,
    level_percent   REAL NOT NULL DEFAULT 0,
    updated_at      REAL NOT NULL,
    PRIMARY KEY (user_id, competency_code)
);

CREATE TABLE IF NOT EXISTS lms_practice_tasks (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    title                TEXT NOT NULL,
    description          TEXT NOT NULL DEFAULT '',
    scenario_id          TEXT NOT NULL,
    category             TEXT NOT NULL DEFAULT 'practice',
    difficulty           TEXT NOT NULL DEFAULT 'MIDDLE',
    duration_min         INTEGER NOT NULL DEFAULT 10,
    required_competencies TEXT NOT NULL DEFAULT '[]',
    is_random            INTEGER NOT NULL DEFAULT 0,
    enabled              INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS lms_user_progress (
    user_id                 INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    module_id               INTEGER NOT NULL REFERENCES lms_course_modules(id) ON DELETE CASCADE,
    status                  TEXT NOT NULL DEFAULT 'NOT_STARTED',
    score                   REAL,
    attempts                INTEGER NOT NULL DEFAULT 0,
    completed_at            REAL,
    last_practice_session_id TEXT,
    PRIMARY KEY (user_id, module_id)
);

CREATE TABLE IF NOT EXISTS lms_notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'info',
    is_read    INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS lms_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS lms_system_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    level     TEXT NOT NULL DEFAULT 'INFO',
    username  TEXT NOT NULL DEFAULT '',
    message   TEXT NOT NULL,
    category  TEXT NOT NULL DEFAULT 'system'
);

CREATE TABLE IF NOT EXISTS lms_chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    author     TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL DEFAULT 'text',
    object_id  TEXT NOT NULL DEFAULT '',
    text       TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lms_chat_session ON lms_chat_messages (session_id, id);

CREATE INDEX IF NOT EXISTS idx_lms_modules_course  ON lms_course_modules (course_id, seq);
CREATE INDEX IF NOT EXISTS idx_lms_progress_user   ON lms_user_progress (user_id);
CREATE INDEX IF NOT EXISTS idx_lms_members_group   ON lms_group_members (group_id);
CREATE INDEX IF NOT EXISTS idx_lms_notify_user     ON lms_notifications (user_id, is_read);
CREATE INDEX IF NOT EXISTS idx_lms_log_time        ON lms_system_log (timestamp);
"""

_SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS lms_groups (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    course_id    INTEGER,
    instructor_id INTEGER,
    created_at   DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS lms_group_members (
    group_id INTEGER NOT NULL REFERENCES lms_groups(id) ON DELETE CASCADE,
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, user_id)
);

CREATE TABLE IF NOT EXISTS lms_courses (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'DRAFT',
    created_at  DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS lms_course_modules (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    course_id   INTEGER NOT NULL REFERENCES lms_courses(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    seq         INTEGER NOT NULL DEFAULT 0,
    content     TEXT NOT NULL DEFAULT '',
    scenario_id TEXT,
    practice_task_id INTEGER
);

CREATE TABLE IF NOT EXISTS lms_competencies (
    code        TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS lms_user_competencies (
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    competency_code TEXT NOT NULL REFERENCES lms_competencies(code) ON DELETE CASCADE,
    level_percent   DOUBLE PRECISION NOT NULL DEFAULT 0,
    updated_at      DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (user_id, competency_code)
);

CREATE TABLE IF NOT EXISTS lms_practice_tasks (
    id                   INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title                TEXT NOT NULL,
    description          TEXT NOT NULL DEFAULT '',
    scenario_id          TEXT NOT NULL,
    category             TEXT NOT NULL DEFAULT 'practice',
    difficulty           TEXT NOT NULL DEFAULT 'MIDDLE',
    duration_min         INTEGER NOT NULL DEFAULT 10,
    required_competencies TEXT NOT NULL DEFAULT '[]',
    is_random            INTEGER NOT NULL DEFAULT 0,
    enabled              INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS lms_user_progress (
    user_id                 INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    module_id               INTEGER NOT NULL REFERENCES lms_course_modules(id) ON DELETE CASCADE,
    status                  TEXT NOT NULL DEFAULT 'NOT_STARTED',
    score                   DOUBLE PRECISION,
    attempts                INTEGER NOT NULL DEFAULT 0,
    completed_at            DOUBLE PRECISION,
    last_practice_session_id TEXT,
    PRIMARY KEY (user_id, module_id)
);

CREATE TABLE IF NOT EXISTS lms_notifications (
    id         INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'info',
    is_read    INTEGER NOT NULL DEFAULT 0,
    created_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS lms_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS lms_system_log (
    id        INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    timestamp DOUBLE PRECISION NOT NULL,
    level     TEXT NOT NULL DEFAULT 'INFO',
    username  TEXT NOT NULL DEFAULT '',
    message   TEXT NOT NULL,
    category  TEXT NOT NULL DEFAULT 'system'
);

CREATE TABLE IF NOT EXISTS lms_chat_messages (
    id         INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id TEXT NOT NULL,
    author     TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL DEFAULT 'text',
    object_id  TEXT NOT NULL DEFAULT '',
    text       TEXT NOT NULL DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lms_chat_session ON lms_chat_messages (session_id, id);

CREATE INDEX IF NOT EXISTS idx_lms_modules_course  ON lms_course_modules (course_id, seq);
CREATE INDEX IF NOT EXISTS idx_lms_progress_user   ON lms_user_progress (user_id);
CREATE INDEX IF NOT EXISTS idx_lms_members_group   ON lms_group_members (group_id);
CREATE INDEX IF NOT EXISTS idx_lms_notify_user     ON lms_notifications (user_id, is_read);
CREATE INDEX IF NOT EXISTS idx_lms_log_time        ON lms_system_log (timestamp);
"""


# Shared with persistence/session_store.py and lms/content_store.py -- see
# persistence/db.py. NOTE: this previously omitted `default=str` (unlike the
# other two copies) -- consolidating onto the shared helper aligns it with
# the majority behavior (falls back to str() for non-JSON-native values
# instead of raising), which only changes behavior on inputs that would
# previously have raised TypeError.
_json = _db.json_dump
_unjson = _db.json_load


class LmsStore:
    """SQLite-backed store for the LMS layer."""

    def __init__(self, path: Optional[Union[Path, str]] = None):
        self._path = path
        self._lock = threading.RLock()
        self._conn = _db.connect(path, DEFAULT_DB_PATH)
        with self._lock:
            self._conn.executescript(
                _SCHEMA if self._conn.dialect == "sqlite" else _SCHEMA_POSTGRES
            )
            self._conn.commit()
        logger.info("LmsStore opened: %s (%s)", self._path, self._conn.dialect)

    @classmethod
    def in_memory(cls) -> "LmsStore":
        return cls(path=":memory:")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "LmsStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Competencies
    # ------------------------------------------------------------------

    def seed_competencies(self, items: List[Competency]) -> None:
        with self._lock, self._conn:
            for c in items:
                self._conn.execute(
                    f"{self._conn.ignore_prefix()} lms_competencies (code, title, description) "
                    f"VALUES (?, ?, ?){self._conn.ignore_suffix('code')}",
                    (c.code, c.title, c.description),
                )
            self._conn.commit()

    def list_competencies(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT code, title, description FROM lms_competencies ORDER BY code"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_competency(self, code: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT code, title, description FROM lms_competencies WHERE code = ?",
                (code,),
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # User competencies
    # ------------------------------------------------------------------

    def get_user_competencies(self, user_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.code, c.title, c.description, u.level_percent, u.updated_at "
                "FROM lms_user_competencies u "
                "JOIN lms_competencies c ON c.code = u.competency_code "
                "WHERE u.user_id = ? ORDER BY c.title",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_user_competency(self, user_id: int, code: str, level: float) -> None:
        level = max(0.0, min(100.0, float(level)))
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO lms_user_competencies (user_id, competency_code, level_percent, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, competency_code) DO UPDATE SET "
                "level_percent = excluded.level_percent, updated_at = excluded.updated_at",
                (user_id, code, level, time.time()),
            )
            self._conn.commit()

    def blend_user_competency(self, user_id: int, code: str, score: float, weight: float = 0.7) -> float:
        """Blend a new practice score into the stored level (EMA-style)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT level_percent FROM lms_user_competencies "
                "WHERE user_id = ? AND competency_code = ?",
                (user_id, code),
            ).fetchone()
        old = float(row["level_percent"]) if row else 0.0
        new = old * (1.0 - weight) + score * weight
        self.set_user_competency(user_id, code, new)
        return new

    def avg_competency_level(self, user_id: int) -> float:
        rows = self.get_user_competencies(user_id)
        if not rows:
            return 0.0
        return sum(float(r["level_percent"]) for r in rows) / len(rows)

    # ------------------------------------------------------------------
    # Courses
    # ------------------------------------------------------------------

    def create_course(self, title: str, description: str = "", status: str = "DRAFT") -> int:
        with self._lock, self._conn:
            new_id = self._conn.insert_returning_id(
                "INSERT INTO lms_courses (title, description, status, created_at) VALUES (?, ?, ?, ?)",
                (title, description, status, time.time()),
            )
            self._conn.commit()
            return new_id

    def update_course(self, course_id: int, fields: Dict[str, Any]) -> None:
        # Column names are filtered through the fixed `allowed` whitelist
        # below before being interpolated, so no external/user-supplied
        # key can ever reach the SQL text.
        allowed = {"title", "description", "status"}
        sets = [f"{k} = ?" for k in fields if k in allowed]
        if not sets:
            return
        params = [fields[k] for k in fields if k in allowed] + [course_id]
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE lms_courses SET {', '.join(sets)} WHERE id = ?", tuple(params)  # nosec B608
            )
            self._conn.commit()

    def get_course(self, course_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM lms_courses WHERE id = ?", (course_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_courses(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM lms_courses"
        params: tuple = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def add_module(self, course_id: int, m: ModuleCreate, seq: Optional[int] = None) -> int:
        with self._lock, self._conn:
            if seq is None:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), -1) + 1 AS next_seq FROM lms_course_modules WHERE course_id = ?",
                    (course_id,),
                ).fetchone()
                seq = int(row["next_seq"])
            new_id = self._conn.insert_returning_id(
                "INSERT INTO lms_course_modules (course_id, kind, title, description, seq, content, "
                "scenario_id, practice_task_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (course_id, m.kind.value if hasattr(m.kind, "value") else m.kind,
                 m.title, m.description, seq, m.content, m.scenario_id, m.practice_task_id),
            )
            self._conn.commit()
            return new_id

    def get_modules(self, course_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM lms_course_modules WHERE course_id = ? ORDER BY seq",
                (course_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_module(self, module_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM lms_course_modules WHERE id = ?", (module_id,)
            ).fetchone()
        return dict(row) if row else None

    def remove_module(self, module_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM lms_course_modules WHERE id = ?", (module_id,))
            self._conn.commit()

    # ------------------------------------------------------------------
    # Practice tasks
    # ------------------------------------------------------------------

    def create_task(self, t: TaskCreate) -> int:
        with self._lock, self._conn:
            new_id = self._conn.insert_returning_id(
                "INSERT INTO lms_practice_tasks (title, description, scenario_id, category, "
                "difficulty, duration_min, required_competencies, is_random, enabled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (t.title, t.description, t.scenario_id,
                 t.category.value if hasattr(t.category, "value") else t.category,
                 t.difficulty.value if hasattr(t.difficulty, "value") else t.difficulty,
                 t.duration_min, _json(t.required_competencies),
                 1 if t.is_random else 0, 1 if t.enabled else 0),
            )
            self._conn.commit()
            return new_id

    def update_task(self, task_id: int, u: TaskUpdate) -> None:
        fields: Dict[str, Any] = {}
        if u.title is not None:
            fields["title"] = u.title
        if u.description is not None:
            fields["description"] = u.description
        if u.scenario_id is not None:
            fields["scenario_id"] = u.scenario_id
        if u.category is not None:
            fields["category"] = u.category.value if hasattr(u.category, "value") else u.category
        if u.difficulty is not None:
            fields["difficulty"] = u.difficulty.value if hasattr(u.difficulty, "value") else u.difficulty
        if u.duration_min is not None:
            fields["duration_min"] = u.duration_min
        if u.required_competencies is not None:
            fields["required_competencies"] = _json(u.required_competencies)
        if u.is_random is not None:
            fields["is_random"] = 1 if u.is_random else 0
        if u.enabled is not None:
            fields["enabled"] = 1 if u.enabled else 0
        if not fields:
            return
        # `fields` keys are literal strings assigned by this function's own
        # code above (never derived from external/user-supplied field
        # names), so there is no injectable column-name path here.
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE lms_practice_tasks SET {sets} WHERE id = ?",  # nosec B608
                tuple(list(fields.values()) + [task_id]),
            )
            self._conn.commit()

    def get_task(self, task_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM lms_practice_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["required_competencies"] = _unjson(d.get("required_competencies"), [])
        return d

    def list_tasks(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM lms_practice_tasks"
        params: tuple = ()
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY category, difficulty, id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["required_competencies"] = _unjson(d.get("required_competencies"), [])
            out.append(d)
        return out

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------

    def create_group(self, g: GroupCreate, instructor_id: int) -> int:
        with self._lock, self._conn:
            new_id = self._conn.insert_returning_id(
                "INSERT INTO lms_groups (name, description, course_id, instructor_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (g.name, g.description, g.course_id, instructor_id, time.time()),
            )
            self._conn.commit()
            return new_id

    def update_group(self, group_id: int, u: GroupUpdate) -> None:
        fields: Dict[str, Any] = {}
        if u.name is not None:
            fields["name"] = u.name
        if u.description is not None:
            fields["description"] = u.description
        if u.course_id is not None:
            fields["course_id"] = u.course_id
        if not fields:
            return
        # `fields` keys are literal strings assigned by this function's own
        # code above (never derived from external/user-supplied field
        # names), so there is no injectable column-name path here.
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE lms_groups SET {sets} WHERE id = ?",  # nosec B608
                tuple(list(fields.values()) + [group_id]),
            )
            self._conn.commit()

    def delete_group(self, group_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM lms_groups WHERE id = ?", (group_id,))
            self._conn.commit()

    def get_group(self, group_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM lms_groups WHERE id = ?", (group_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_groups(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM lms_groups ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def set_members(self, group_id: int, user_ids: List[int]) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM lms_group_members WHERE group_id = ?", (group_id,))
            for uid in user_ids:
                self._conn.execute(
                    f"{self._conn.ignore_prefix()} lms_group_members (group_id, user_id) VALUES (?, ?)"
                    f"{self._conn.ignore_suffix('group_id, user_id')}",
                    (group_id, uid),
                )
            self._conn.commit()

    def add_members(self, group_id: int, user_ids: List[int]) -> None:
        with self._lock, self._conn:
            for uid in user_ids:
                self._conn.execute(
                    f"{self._conn.ignore_prefix()} lms_group_members (group_id, user_id) VALUES (?, ?)"
                    f"{self._conn.ignore_suffix('group_id, user_id')}",
                    (group_id, uid),
                )
            self._conn.commit()

    def remove_members(self, group_id: int, user_ids: List[int]) -> None:
        with self._lock, self._conn:
            for uid in user_ids:
                self._conn.execute(
                    "DELETE FROM lms_group_members WHERE group_id = ? AND user_id = ?",
                    (group_id, uid),
                )
            self._conn.commit()

    def member_ids(self, group_id: int) -> List[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id FROM lms_group_members WHERE group_id = ? ORDER BY user_id",
                (group_id,),
            ).fetchall()
        return [int(r["user_id"]) for r in rows]

    def group_member_count(self, group_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM lms_group_members WHERE group_id = ?",
                (group_id,),
            ).fetchone()
        return int(row["n"])

    def groups_for_user(self, user_id: int) -> List[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT group_id FROM lms_group_members WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        return [int(r["group_id"]) for r in rows]

    def all_members_users(self) -> List[int]:
        with self._lock:
            rows = self._conn.execute("SELECT DISTINCT user_id FROM lms_group_members").fetchall()
        return [int(r["user_id"]) for r in rows]

    # ------------------------------------------------------------------
    # User progress
    # ------------------------------------------------------------------

    def get_progress(self, user_id: int, module_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM lms_user_progress WHERE user_id = ? AND module_id = ?",
                (user_id, module_id),
            ).fetchone()
        return dict(row) if row else None

    def get_all_progress(self, user_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM lms_user_progress WHERE user_id = ?", (user_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def upsert_progress(self, p: UserProgress) -> None:
        status = p.status.value if hasattr(p.status, "value") else p.status
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO lms_user_progress (user_id, module_id, status, score, attempts, "
                "completed_at, last_practice_session_id) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, module_id) DO UPDATE SET "
                "status = excluded.status, score = excluded.score, attempts = excluded.attempts, "
                "completed_at = excluded.completed_at, "
                "last_practice_session_id = excluded.last_practice_session_id",
                (p.user_id, p.module_id, status, p.score, p.attempts,
                 p.completed_at, p.last_practice_session_id),
            )
            self._conn.commit()

    def mark_module_started(self, user_id: int, module_id: int) -> UserProgress:
        p = self.get_progress(user_id, module_id)
        if p is None:
            p = UserProgress(user_id=user_id, module_id=module_id,
                             status=ModuleStatus.IN_PROGRESS, attempts=1)
            self.upsert_progress(p)
            return p
        if p["status"] in ("NOT_STARTED", "IN_PROGRESS"):
            p["status"] = "IN_PROGRESS"
            p["attempts"] = int(p.get("attempts", 0)) + 1
            self.upsert_progress(UserProgress(
                user_id=user_id, module_id=module_id, status=ModuleStatus.IN_PROGRESS,
                score=p.get("score"), attempts=p["attempts"],
                completed_at=p.get("completed_at"),
                last_practice_session_id=p.get("last_practice_session_id"),
            ))
        return UserProgress(**{**{k: v for k, v in p.items() if k != "status"}, "status": ModuleStatus(p["status"])})

    def mark_module_completed(self, user_id: int, module_id: int, score: float,
                              session_id: Optional[str] = None) -> UserProgress:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM lms_user_progress WHERE user_id = ? AND module_id = ?",
                (user_id, module_id),
            ).fetchone()
        attempts = int(row["attempts"]) + 1 if row else 1
        p = UserProgress(
            user_id=user_id, module_id=module_id, status=ModuleStatus.COMPLETED,
            score=score, attempts=attempts, completed_at=time.time(),
            last_practice_session_id=session_id,
        )
        self.upsert_progress(p)
        return p

    def set_theory_completed(self, user_id: int, module_id: int) -> UserProgress:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM lms_user_progress WHERE user_id = ? AND module_id = ?",
                (user_id, module_id),
            ).fetchone()
        attempts = int(row["attempts"]) if row else 1
        p = UserProgress(
            user_id=user_id, module_id=module_id, status=ModuleStatus.COMPLETED,
            score=row["score"] if row else None, attempts=attempts,
            completed_at=time.time(), last_practice_session_id=row["last_practice_session_id"] if row else None,
        )
        self.upsert_progress(p)
        return p

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    def add_notification(self, user_id: int, text: str, kind: str = "info") -> int:
        with self._lock, self._conn:
            new_id = self._conn.insert_returning_id(
                "INSERT INTO lms_notifications (user_id, text, kind, is_read, created_at) "
                "VALUES (?, ?, ?, 0, ?)",
                (user_id, text, kind, time.time()),
            )
            self._conn.commit()
            return new_id

    def list_notifications(self, user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM lms_notifications WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_notifications_read(self, user_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE lms_notifications SET is_read = 1 WHERE user_id = ?", (user_id,)
            )
            self._conn.commit()

    def unread_notification_count(self, user_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM lms_notifications WHERE user_id = ? AND is_read = 0",
                (user_id,),
            ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def get_settings(self) -> Dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM lms_settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def set_settings(self, values: Dict[str, str]) -> None:
        with self._lock, self._conn:
            for k, v in values.items():
                self._conn.execute(
                    "INSERT INTO lms_settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (k, v),
                )
            self._conn.commit()

    def seed_settings(self, defaults: Dict[str, str]) -> None:
        with self._lock, self._conn:
            for k, v in defaults.items():
                self._conn.execute(
                    f"{self._conn.ignore_prefix()} lms_settings (key, value) VALUES (?, ?)"
                    f"{self._conn.ignore_suffix('key')}",
                    (k, v),
                )
            self._conn.commit()

    # ------------------------------------------------------------------
    # System log
    # ------------------------------------------------------------------

    def add_log(self, message: str, level: str = "INFO", username: str = "",
                category: str = "system") -> int:
        with self._lock, self._conn:
            new_id = self._conn.insert_returning_id(
                "INSERT INTO lms_system_log (timestamp, level, username, message, category) "
                "VALUES (?, ?, ?, ?, ?)",
                (time.time(), level, username, message, category),
            )
            self._conn.commit()
            return new_id

    def list_logs(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM lms_system_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Practice chat (multi-operator)
    # ------------------------------------------------------------------

    def add_chat_message(self, session_id: str, author: str, kind: str,
                         object_id: str, text: str) -> Dict[str, Any]:
        now = time.time()
        with self._lock, self._conn:
            new_id = self._conn.insert_returning_id(
                "INSERT INTO lms_chat_messages (session_id, author, kind, object_id, text, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, author, kind, object_id, text, now),
            )
            self._conn.commit()
            return {
                "id": new_id,
                "session_id": session_id,
                "author": author,
                "kind": kind,
                "object_id": object_id,
                "text": text,
                "created_at": now,
            }

    def list_chat_messages(self, session_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM lms_chat_messages WHERE session_id = ? ORDER BY id ASC", (session_id,)
            ).fetchall()
        return [dict(r) for r in rows]
