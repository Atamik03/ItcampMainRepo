"""
lms/content_store.py
====================
SQLite storage for the authoring & study system («Обуч.txt»).

Shares the same `sessions.db` file as the rest of the LMS and opens its own
connection (WAL allows concurrent readers/writers across connections).

New tables:
    lms_lessons         theory lessons with content blocks
    lms_tests           test configuration per module
    lms_questions       questions of a test
    lms_training_tasks  practical task spec (start/target state, restrictions)
    lms_scenarios       dynamic scenario timeline + statuses
    lms_assessments     results of tests / practices / exams
    lms_action_log      журнал действий оператора (по «Обуч.txt» §12)

Also adds the `published` flag to lms_course_modules (модуль: черновик/опубликован).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from persistence import db as _db

from .content_models import (
    Assessment,
    LessonWrite,
    QuestionWrite,
    ScenarioWrite,
    TestWrite,
    TaskWrite,
)

logger = logging.getLogger("elou_avt.lms_content")

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "sessions.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lms_lessons (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id        INTEGER NOT NULL REFERENCES lms_course_modules(id) ON DELETE CASCADE,
    title            TEXT NOT NULL,
    seq              INTEGER NOT NULL DEFAULT 0,
    blocks           TEXT NOT NULL DEFAULT '[]',
    equipment_ids    TEXT NOT NULL DEFAULT '[]',
    competency_codes TEXT NOT NULL DEFAULT '[]',
    created_at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS lms_tests (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id        INTEGER NOT NULL UNIQUE REFERENCES lms_course_modules(id) ON DELETE CASCADE,
    title            TEXT NOT NULL DEFAULT 'Контроль знаний',
    passing_score    REAL NOT NULL DEFAULT 70,
    attempts         INTEGER NOT NULL DEFAULT 0,
    retry_required   INTEGER NOT NULL DEFAULT 0,
    shuffle          INTEGER NOT NULL DEFAULT 0,
    competency_codes TEXT NOT NULL DEFAULT '[]',
    created_at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS lms_questions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id   INTEGER NOT NULL REFERENCES lms_tests(id) ON DELETE CASCADE,
    kind      TEXT NOT NULL,
    title     TEXT NOT NULL,
    text      TEXT NOT NULL DEFAULT '',
    seq       INTEGER NOT NULL DEFAULT 0,
    options   TEXT NOT NULL DEFAULT '[]',
    answer    TEXT,
    max_score REAL NOT NULL DEFAULT 1,
    penalty   REAL NOT NULL DEFAULT 0,
    required  INTEGER NOT NULL DEFAULT 1,
    hint      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS lms_training_tasks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id        INTEGER NOT NULL UNIQUE REFERENCES lms_course_modules(id) ON DELETE CASCADE,
    title            TEXT NOT NULL,
    goal             TEXT NOT NULL DEFAULT '',
    scenario_id      TEXT NOT NULL DEFAULT '',
    duration_min     INTEGER NOT NULL DEFAULT 10,
    initial_state    TEXT NOT NULL DEFAULT '{}',
    target_state     TEXT NOT NULL DEFAULT '[]',
    restrictions     TEXT NOT NULL DEFAULT '[]',
    criteria         TEXT NOT NULL DEFAULT '[]',
    expected_actions TEXT NOT NULL DEFAULT '[]',
    critical_errors  TEXT NOT NULL DEFAULT '[]',
    competency_codes TEXT NOT NULL DEFAULT '[]',
    equipment_ids    TEXT NOT NULL DEFAULT '[]',
    enabled          INTEGER NOT NULL DEFAULT 1,
    created_at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS lms_scenarios (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id        INTEGER NOT NULL UNIQUE REFERENCES lms_course_modules(id) ON DELETE CASCADE,
    title            TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    goal             TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'DRAFT',
    initial_state    TEXT NOT NULL DEFAULT '{}',
    events           TEXT NOT NULL DEFAULT '[]',
    expected_actions TEXT NOT NULL DEFAULT '[]',
    success_criteria TEXT NOT NULL DEFAULT '[]',
    critical_errors  TEXT NOT NULL DEFAULT '[]',
    target_state     TEXT NOT NULL DEFAULT '[]',
    field_errors     TEXT NOT NULL DEFAULT '[]',
    final_state      TEXT NOT NULL DEFAULT '{}',
    competency_codes TEXT NOT NULL DEFAULT '[]',
    equipment_ids    TEXT NOT NULL DEFAULT '[]',
    duration_min     INTEGER NOT NULL DEFAULT 10,
    is_exam          INTEGER NOT NULL DEFAULT 0,
    multi_operator   INTEGER NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS lms_assessments (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id              INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    module_id            INTEGER NOT NULL REFERENCES lms_course_modules(id) ON DELETE CASCADE,
    kind                 TEXT NOT NULL,
    test_id              INTEGER,
    task_id              INTEGER,
    scenario_id          TEXT,
    score                REAL NOT NULL DEFAULT 0,
    max_score            REAL NOT NULL DEFAULT 100,
    passed               INTEGER NOT NULL DEFAULT 0,
    criteria_scores      TEXT NOT NULL DEFAULT '{}',
    errors_count         INTEGER NOT NULL DEFAULT 0,
    critical_errors_count INTEGER NOT NULL DEFAULT 0,
    duration_s           REAL NOT NULL DEFAULT 0,
    answers              TEXT,
    feedback_good        TEXT NOT NULL DEFAULT '[]',
    feedback_bad         TEXT NOT NULL DEFAULT '[]',
    session_id           TEXT,
    started_at           REAL NOT NULL,
    finished_at          REAL NOT NULL,
    created_at           REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS lms_action_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   REAL NOT NULL,
    user_id     INTEGER,
    username    TEXT NOT NULL DEFAULT '',
    object_id   TEXT NOT NULL DEFAULT '',
    object_name TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL DEFAULT '',
    old_state   TEXT NOT NULL DEFAULT '{}',
    new_state   TEXT NOT NULL DEFAULT '{}',
    source      TEXT NOT NULL DEFAULT 'operator_panel',
    session_id  TEXT,
    module_id   INTEGER
);

CREATE TABLE IF NOT EXISTS lms_scada_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   REAL NOT NULL,
    user_id     INTEGER,
    username    TEXT NOT NULL DEFAULT '',
    event_type  TEXT NOT NULL DEFAULT 'click',
    object_id   TEXT NOT NULL DEFAULT '',
    object_name TEXT NOT NULL DEFAULT '',
    duration_s  REAL,
    session_id  TEXT,
    module_id   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_lessons_module  ON lms_lessons (module_id, seq);
CREATE INDEX IF NOT EXISTS idx_questions_test  ON lms_questions (test_id, seq);
CREATE INDEX IF NOT EXISTS idx_tasks_module    ON lms_training_tasks (module_id);
CREATE INDEX IF NOT EXISTS idx_scenarios_mod   ON lms_scenarios (module_id);
CREATE INDEX IF NOT EXISTS idx_assess_user     ON lms_assessments (user_id, finished_at);
CREATE INDEX IF NOT EXISTS idx_assess_module   ON lms_assessments (module_id);
CREATE INDEX IF NOT EXISTS idx_action_log_time ON lms_action_log (timestamp);
CREATE INDEX IF NOT EXISTS idx_scada_log_time  ON lms_scada_log (timestamp);
CREATE INDEX IF NOT EXISTS idx_scada_log_user  ON lms_scada_log (username, timestamp);
"""

_SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS lms_lessons (
    id               INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    module_id        INTEGER NOT NULL REFERENCES lms_course_modules(id) ON DELETE CASCADE,
    title            TEXT NOT NULL,
    seq              INTEGER NOT NULL DEFAULT 0,
    blocks           TEXT NOT NULL DEFAULT '[]',
    equipment_ids    TEXT NOT NULL DEFAULT '[]',
    competency_codes TEXT NOT NULL DEFAULT '[]',
    created_at       DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS lms_tests (
    id               INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    module_id        INTEGER NOT NULL UNIQUE REFERENCES lms_course_modules(id) ON DELETE CASCADE,
    title            TEXT NOT NULL DEFAULT 'Контроль знаний',
    passing_score    DOUBLE PRECISION NOT NULL DEFAULT 70,
    attempts         INTEGER NOT NULL DEFAULT 0,
    retry_required   INTEGER NOT NULL DEFAULT 0,
    shuffle          INTEGER NOT NULL DEFAULT 0,
    competency_codes TEXT NOT NULL DEFAULT '[]',
    created_at       DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS lms_questions (
    id        INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    test_id   INTEGER NOT NULL REFERENCES lms_tests(id) ON DELETE CASCADE,
    kind      TEXT NOT NULL,
    title     TEXT NOT NULL,
    text      TEXT NOT NULL DEFAULT '',
    seq       INTEGER NOT NULL DEFAULT 0,
    options   TEXT NOT NULL DEFAULT '[]',
    answer    TEXT,
    max_score DOUBLE PRECISION NOT NULL DEFAULT 1,
    penalty   DOUBLE PRECISION NOT NULL DEFAULT 0,
    required  INTEGER NOT NULL DEFAULT 1,
    hint      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS lms_training_tasks (
    id               INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    module_id        INTEGER NOT NULL UNIQUE REFERENCES lms_course_modules(id) ON DELETE CASCADE,
    title            TEXT NOT NULL,
    goal             TEXT NOT NULL DEFAULT '',
    scenario_id      TEXT NOT NULL DEFAULT '',
    duration_min     INTEGER NOT NULL DEFAULT 10,
    initial_state    TEXT NOT NULL DEFAULT '{}',
    target_state     TEXT NOT NULL DEFAULT '[]',
    restrictions     TEXT NOT NULL DEFAULT '[]',
    criteria         TEXT NOT NULL DEFAULT '[]',
    expected_actions TEXT NOT NULL DEFAULT '[]',
    critical_errors  TEXT NOT NULL DEFAULT '[]',
    competency_codes TEXT NOT NULL DEFAULT '[]',
    equipment_ids    TEXT NOT NULL DEFAULT '[]',
    enabled          INTEGER NOT NULL DEFAULT 1,
    created_at       DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS lms_scenarios (
    id               INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    module_id        INTEGER NOT NULL UNIQUE REFERENCES lms_course_modules(id) ON DELETE CASCADE,
    title            TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    goal             TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'DRAFT',
    initial_state    TEXT NOT NULL DEFAULT '{}',
    events           TEXT NOT NULL DEFAULT '[]',
    expected_actions TEXT NOT NULL DEFAULT '[]',
    success_criteria TEXT NOT NULL DEFAULT '[]',
    critical_errors  TEXT NOT NULL DEFAULT '[]',
    target_state     TEXT NOT NULL DEFAULT '[]',
    field_errors     TEXT NOT NULL DEFAULT '[]',
    final_state      TEXT NOT NULL DEFAULT '{}',
    competency_codes TEXT NOT NULL DEFAULT '[]',
    equipment_ids    TEXT NOT NULL DEFAULT '[]',
    duration_min     INTEGER NOT NULL DEFAULT 10,
    is_exam          INTEGER NOT NULL DEFAULT 0,
    multi_operator   INTEGER NOT NULL DEFAULT 0,
    created_at       DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS lms_assessments (
    id                   INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id              INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    module_id            INTEGER NOT NULL REFERENCES lms_course_modules(id) ON DELETE CASCADE,
    kind                 TEXT NOT NULL,
    test_id              INTEGER,
    task_id              INTEGER,
    scenario_id          TEXT,
    score                DOUBLE PRECISION NOT NULL DEFAULT 0,
    max_score            DOUBLE PRECISION NOT NULL DEFAULT 100,
    passed               INTEGER NOT NULL DEFAULT 0,
    criteria_scores      TEXT NOT NULL DEFAULT '{}',
    errors_count         INTEGER NOT NULL DEFAULT 0,
    critical_errors_count INTEGER NOT NULL DEFAULT 0,
    duration_s           DOUBLE PRECISION NOT NULL DEFAULT 0,
    answers              TEXT,
    feedback_good        TEXT NOT NULL DEFAULT '[]',
    feedback_bad         TEXT NOT NULL DEFAULT '[]',
    session_id           TEXT,
    started_at           DOUBLE PRECISION NOT NULL,
    finished_at          DOUBLE PRECISION NOT NULL,
    created_at           DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS lms_action_log (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    timestamp   DOUBLE PRECISION NOT NULL,
    user_id     INTEGER,
    username    TEXT NOT NULL DEFAULT '',
    object_id   TEXT NOT NULL DEFAULT '',
    object_name TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL DEFAULT '',
    old_state   TEXT NOT NULL DEFAULT '{}',
    new_state   TEXT NOT NULL DEFAULT '{}',
    source      TEXT NOT NULL DEFAULT 'operator_panel',
    session_id  TEXT,
    module_id   INTEGER
);

CREATE TABLE IF NOT EXISTS lms_scada_log (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    timestamp   DOUBLE PRECISION NOT NULL,
    user_id     INTEGER,
    username    TEXT NOT NULL DEFAULT '',
    event_type  TEXT NOT NULL DEFAULT 'click',
    object_id   TEXT NOT NULL DEFAULT '',
    object_name TEXT NOT NULL DEFAULT '',
    duration_s  DOUBLE PRECISION,
    session_id  TEXT,
    module_id   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_lessons_module  ON lms_lessons (module_id, seq);
CREATE INDEX IF NOT EXISTS idx_questions_test  ON lms_questions (test_id, seq);
CREATE INDEX IF NOT EXISTS idx_tasks_module    ON lms_training_tasks (module_id);
CREATE INDEX IF NOT EXISTS idx_scenarios_mod   ON lms_scenarios (module_id);
CREATE INDEX IF NOT EXISTS idx_assess_user     ON lms_assessments (user_id, finished_at);
CREATE INDEX IF NOT EXISTS idx_assess_module   ON lms_assessments (module_id);
CREATE INDEX IF NOT EXISTS idx_action_log_time ON lms_action_log (timestamp);
CREATE INDEX IF NOT EXISTS idx_scada_log_time  ON lms_scada_log (timestamp);
CREATE INDEX IF NOT EXISTS idx_scada_log_user  ON lms_scada_log (username, timestamp);
"""


# Shared with persistence/session_store.py and lms/store.py -- see persistence/db.py.
_json = _db.json_dump
_unjson = _db.json_load


def _normalize_question_data(q: Dict[str, Any]) -> tuple:
    """Return (options, answer) in the canonical frontend/backend format.

    Canonical format:
        single/multi  options [{key,label}]   answer [key] / [key,...]
        match         options [{left,right}]  answer [{left,right}] (same pairs)
        sequence      options [{label}]       answer [labels in order]
        object        options [{key,label}]   answer [key]
    """
    kind = q.get("kind", "single")
    options = q.get("options") or []
    answer = q.get("answer")

    if kind == "match":
        pairs = answer if isinstance(answer, list) and answer else options
        return list(pairs), list(pairs)

    if kind == "sequence":
        labels = [{"label": str(o.get("label", ""))} for o in options if o.get("label")]
        seq = [str(x) for x in answer] if isinstance(answer, list) else []
        return labels, seq

    if kind == "object":
        new_opts = []
        for o in options:
            key = str(o.get("key", "") or o.get("label", ""))
            label = str(o.get("label", "") or o.get("key", ""))
            new_opts.append({"key": key, "label": label})
        key = str(answer[0]) if isinstance(answer, list) and answer else str(answer or "")
        return new_opts, [key]

    # single / multi
    new_opts = []
    keys = []
    for i, o in enumerate(options):
        if "key" in o:
            key = str(o.get("key", ""))
            label = str(o.get("label", "") or key)
            new_opts.append({"key": key, "label": label})
        else:
            key = f"opt{i + 1}"
            label = str(o.get("label", ""))
            new_opts.append({"key": key, "label": label})
        keys.append(key)

    if kind == "single":
        if isinstance(answer, list) and answer and all(isinstance(a, str) and a in keys for a in answer):
            correct = [a for a in answer if a in keys]
        elif isinstance(answer, str) and answer in keys:
            correct = [answer]
        else:
            legacy = {str(o.get("label", "")): str(o.get("key", f"opt{idx + 1}"))
                      for idx, o in enumerate(options)}
            correct_keys = [str(o.get("key", f"opt{idx + 1}")) for idx, o in enumerate(options)
                            if o.get("is_correct")]
            if not correct_keys:
                target = answer if isinstance(answer, list) else ([answer] if answer is not None else [])
                correct_keys = [legacy.get(str(t), "") for t in target]
            correct = correct_keys
        return new_opts, correct

    # multi
    if isinstance(answer, list) and all(isinstance(a, str) and a in keys for a in answer):
        correct = [a for a in answer if a in keys]
    else:
        legacy = {str(o.get("label", "")): str(o.get("key", f"opt{idx + 1}"))
                  for idx, o in enumerate(options)}
        correct_keys = [str(o.get("key", f"opt{idx + 1}")) for idx, o in enumerate(options)
                        if o.get("is_correct")]
        if not correct_keys and isinstance(answer, list):
            correct_keys = [legacy.get(str(t), "") for t in answer]
        correct = correct_keys
    return new_opts, correct


class LmsContentStore:
    """SQLite-backed store for the authoring & study system."""

    def __init__(self, path: Optional[Union[Path, str]] = None):
        self._path = path
        self._lock = threading.RLock()
        self._conn = _db.connect(path, DEFAULT_DB_PATH)
        with self._lock:
            self._conn.executescript(
                _SCHEMA if self._conn.dialect == "sqlite" else _SCHEMA_POSTGRES
            )
            self._migrate_module_published()
            self._migrate_scenario_target_state()
            self._migrate_scenario_field_errors()
            self._migrate_scenario_multi_operator()
            self._conn.commit()
        logger.info("LmsContentStore opened: %s (%s)", self._path, self._conn.dialect)

    @classmethod
    def in_memory(cls) -> "LmsContentStore":
        return cls(path=":memory:")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate_module_published(self) -> None:
        # Postgres always gets `published` from _SCHEMA_POSTGRES on first
        # create; PRAGMA table_info / ALTER TABLE ADD COLUMN below are
        # SQLite-only runtime migrations for pre-existing local dev DBs.
        if self._conn.dialect != "sqlite":
            return
        import sqlite3

        cols = {r["name"] for r in self._conn.execute(
            "PRAGMA table_info(lms_course_modules)").fetchall()}
        if "published" not in cols:
            try:
                self._conn.execute(
                    "ALTER TABLE lms_course_modules ADD COLUMN published INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                logger.debug("migration published: column already exists")

    def _migrate_scenario_target_state(self) -> None:
        if self._conn.dialect != "sqlite":
            return
        import sqlite3

        cols = {r["name"] for r in self._conn.execute(
            "PRAGMA table_info(lms_scenarios)").fetchall()}
        if "target_state" not in cols:
            try:
                self._conn.execute(
                    "ALTER TABLE lms_scenarios ADD COLUMN target_state TEXT NOT NULL DEFAULT '[]'")
            except sqlite3.OperationalError:
                logger.debug("migration target_state: column already exists")

    def _migrate_scenario_field_errors(self) -> None:
        if self._conn.dialect != "sqlite":
            return
        import sqlite3

        cols = {r["name"] for r in self._conn.execute(
            "PRAGMA table_info(lms_scenarios)").fetchall()}
        if "field_errors" not in cols:
            try:
                self._conn.execute(
                    "ALTER TABLE lms_scenarios ADD COLUMN field_errors TEXT NOT NULL DEFAULT '[]'")
            except sqlite3.OperationalError:
                logger.debug("migration field_errors: column already exists")

    def _migrate_scenario_multi_operator(self) -> None:
        if self._conn.dialect != "sqlite":
            return
        import sqlite3

        cols = {r["name"] for r in self._conn.execute(
            "PRAGMA table_info(lms_scenarios)").fetchall()}
        if "multi_operator" not in cols:
            try:
                self._conn.execute(
                    "ALTER TABLE lms_scenarios ADD COLUMN multi_operator INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                logger.debug("migration multi_operator: column already exists")

    # ------------------------------------------------------------------
    # Module helpers
    # ------------------------------------------------------------------

    def module(self, module_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM lms_course_modules WHERE id = ?", (module_id,)
            ).fetchone()
        return dict(row) if row else None

    def set_module_published(self, module_id: int, published: bool) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE lms_course_modules SET published = ? WHERE id = ?",
                (1 if published else 0, module_id),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Lessons
    # ------------------------------------------------------------------

    def create_lesson(self, module_id: int, w: LessonWrite, seq: Optional[int] = None) -> int:
        with self._lock, self._conn:
            if seq is None:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), -1) + 1 AS next_seq FROM lms_lessons WHERE module_id = ?",
                    (module_id,),
                ).fetchone()
                seq = int(row["next_seq"])
            new_id = self._conn.insert_returning_id(
                "INSERT INTO lms_lessons (module_id, title, seq, blocks, equipment_ids, "
                "competency_codes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (module_id, w.title, seq, _json([b.model_dump() for b in w.blocks]),
                 _json(w.equipment_ids), _json(w.competency_codes), time.time()),
            )
            self._conn.commit()
            return new_id

    def update_lesson(self, lesson_id: int, w: LessonWrite) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE lms_lessons SET title = ?, blocks = ?, equipment_ids = ?, "
                "competency_codes = ? WHERE id = ?",
                (w.title, _json([b.model_dump() for b in w.blocks]),
                 _json(w.equipment_ids), _json(w.competency_codes), lesson_id),
            )
            self._conn.commit()

    def delete_lesson(self, lesson_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM lms_lessons WHERE id = ?", (lesson_id,))
            self._conn.commit()

    def get_lesson(self, lesson_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM lms_lessons WHERE id = ?", (lesson_id,)).fetchone()
        if row is None:
            return None
        return self._decode_lesson(dict(row))

    def list_lessons(self, module_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM lms_lessons WHERE module_id = ? ORDER BY seq", (module_id,)
            ).fetchall()
        return [self._decode_lesson(dict(r)) for r in rows]

    @staticmethod
    def _decode_lesson(d: Dict[str, Any]) -> Dict[str, Any]:
        d["blocks"] = _unjson(d.get("blocks"), [])
        d["equipment_ids"] = _unjson(d.get("equipment_ids"), [])
        d["competency_codes"] = _unjson(d.get("competency_codes"), [])
        return d

    # ------------------------------------------------------------------
    # Tests & questions
    # ------------------------------------------------------------------

    def upsert_test(self, module_id: int, w: TestWrite) -> int:
        # Single atomic INSERT ... ON CONFLICT(module_id) DO UPDATE ... RETURNING id
        # instead of SELECT-then-branch: safe under concurrent writers on
        # Postgres (the in-process RLock only serializes this one process).
        # `module_id` carries a UNIQUE constraint in both _SCHEMA and
        # _SCHEMA_POSTGRES for lms_tests. Syntax is valid on SQLite 3.35+
        # and Postgres alike, so no dialect branch is needed here.
        with self._lock, self._conn:
            row = self._conn.execute(
                "INSERT INTO lms_tests (module_id, title, passing_score, attempts, "
                "retry_required, shuffle, competency_codes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(module_id) DO UPDATE SET "
                "title = excluded.title, passing_score = excluded.passing_score, "
                "attempts = excluded.attempts, retry_required = excluded.retry_required, "
                "shuffle = excluded.shuffle, competency_codes = excluded.competency_codes "
                "RETURNING id",
                (module_id, w.title, w.passing_score, w.attempts,
                 1 if w.retry_required else 0, 1 if w.shuffle else 0,
                 _json(w.competency_codes), time.time()),
            ).fetchone()
            self._conn.commit()
            return int(row["id"])

    def delete_test(self, test_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM lms_tests WHERE id = ?", (test_id,))
            self._conn.commit()

    def list_tests_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM lms_tests ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["competency_codes"] = _unjson(d.get("competency_codes"), [])
            d["retry_required"] = bool(d["retry_required"])
            d["shuffle"] = bool(d["shuffle"])
            out.append(d)
        return out

    def get_test(self, test_id: int, with_answers: bool = True) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM lms_tests WHERE id = ?", (test_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["competency_codes"] = _unjson(d.get("competency_codes"), [])
        d["retry_required"] = bool(d["retry_required"])
        d["shuffle"] = bool(d["shuffle"])
        questions = self.list_questions(test_id, with_answers=with_answers)
        if not with_answers:
            for q in questions:
                q.pop("answer", None)
                for o in q.get("options", []):
                    o.pop("is_correct", None)
        d["questions"] = questions
        return d

    def get_test_by_module(self, module_id: int, with_answers: bool = True) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM lms_tests WHERE module_id = ?", (module_id,)
            ).fetchone()
        return self.get_test(int(row["id"]), with_answers=with_answers) if row else None

    def create_question(self, test_id: int, w: QuestionWrite) -> int:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 AS next_seq FROM lms_questions WHERE test_id = ?",
                (test_id,),
            ).fetchone()
            seq = int(row["next_seq"])
            new_id = self._conn.insert_returning_id(
                "INSERT INTO lms_questions (test_id, kind, title, text, seq, options, answer, "
                "max_score, penalty, required, hint) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (test_id, w.kind.value, w.title, w.text, seq, _json(w.options), _json(w.answer),
                 w.max_score, w.penalty, 1 if w.required else 0, w.hint),
            )
            self._conn.commit()
            return new_id

    def update_question(self, question_id: int, w: QuestionWrite) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE lms_questions SET kind = ?, title = ?, text = ?, options = ?, "
                "answer = ?, max_score = ?, penalty = ?, required = ?, hint = ? WHERE id = ?",
                (w.kind.value, w.title, w.text, _json(w.options), _json(w.answer),
                 w.max_score, w.penalty, 1 if w.required else 0, w.hint, question_id),
            )
            self._conn.commit()

    def delete_question(self, question_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM lms_questions WHERE id = ?", (question_id,))
            self._conn.commit()

    def get_question(self, question_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM lms_questions WHERE id = ?", (question_id,)).fetchone()
        if row is None:
            return None
        return self._decode_question(dict(row))

    def list_questions(self, test_id: int, with_answers: bool = True) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM lms_questions WHERE test_id = ? ORDER BY seq", (test_id,)
            ).fetchall()
        out = [self._decode_question(dict(r)) for r in rows]
        if not with_answers:
            for q in out:
                q.pop("answer", None)
                for o in q.get("options", []):
                    o.pop("is_correct", None)
        return out

    @staticmethod
    def _decode_question(d: Dict[str, Any]) -> Dict[str, Any]:
        d["options"] = _unjson(d.get("options"), [])
        d["answer"] = _unjson(d.get("answer"))
        d["required"] = bool(d["required"])
        return d

    def migrate_question_formats(self) -> int:
        """Convert legacy question data to the canonical {key,label} format.

        single/multi: options [{label,is_correct}] -> [{key,label}], answer -> [keys]
        match:        options [] -> answer (pairs [{left,right}])
        sequence:     options [{label,is_correct}] -> [{label}], answer unchanged
        object:       options [{label}] -> [{key,label}], answer -> [key]

        Idempotent: rows already in the canonical format are left untouched.
        """
        changed = 0
        with self._lock:
            rows = self._conn.execute("SELECT * FROM lms_questions").fetchall()
            for r in rows:
                q = self._decode_question(dict(r))
                new_options, new_answer = _normalize_question_data(q)
                if new_options == q["options"] and new_answer == q["answer"]:
                    continue
                self._conn.execute(
                    "UPDATE lms_questions SET options = ?, answer = ? WHERE id = ?",
                    (_json(new_options), _json(new_answer), q["id"]),
                )
                changed += 1
            self._conn.commit()
        return changed

    # ------------------------------------------------------------------
    # Training tasks
    # ------------------------------------------------------------------

    def upsert_task(self, module_id: int, w: TaskWrite) -> int:
        # Atomic INSERT ... ON CONFLICT(module_id) DO UPDATE ... RETURNING id;
        # see upsert_test for rationale. `module_id` is UNIQUE on
        # lms_training_tasks in both _SCHEMA and _SCHEMA_POSTGRES.
        with self._lock, self._conn:
            values = (
                w.title, w.goal, w.scenario_id, w.duration_min, _json(w.initial_state),
                _json([c.model_dump() for c in w.target_state]),
                _json([r.model_dump() for r in w.restrictions]),
                _json([c.model_dump() for c in w.criteria]),
                _json([a.model_dump() for a in w.expected_actions]),
                _json([r.model_dump() for r in w.critical_errors]),
                _json(w.competency_codes), _json(w.equipment_ids), 1 if w.enabled else 0,
            )
            row = self._conn.execute(
                "INSERT INTO lms_training_tasks (module_id, title, goal, scenario_id, duration_min, "
                "initial_state, target_state, restrictions, criteria, expected_actions, "
                "critical_errors, competency_codes, equipment_ids, enabled, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(module_id) DO UPDATE SET "
                "title = excluded.title, goal = excluded.goal, scenario_id = excluded.scenario_id, "
                "duration_min = excluded.duration_min, initial_state = excluded.initial_state, "
                "target_state = excluded.target_state, restrictions = excluded.restrictions, "
                "criteria = excluded.criteria, expected_actions = excluded.expected_actions, "
                "critical_errors = excluded.critical_errors, competency_codes = excluded.competency_codes, "
                "equipment_ids = excluded.equipment_ids, enabled = excluded.enabled "
                "RETURNING id",
                (module_id,) + values + (time.time(),),
            ).fetchone()
            self._conn.commit()
            return int(row["id"])

    def delete_task(self, task_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM lms_training_tasks WHERE id = ?", (task_id,))
            self._conn.commit()

    def get_task(self, task_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM lms_training_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._decode_task(dict(row)) if row else None

    def get_task_by_module(self, module_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM lms_training_tasks WHERE module_id = ?", (module_id,)
            ).fetchone()
        return self.get_task(int(row["id"])) if row else None

    def list_tasks_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM lms_training_tasks ORDER BY id"
            ).fetchall()
        return [self._decode_task(dict(r)) for r in rows]

    @staticmethod
    def _decode_task(d: Dict[str, Any]) -> Dict[str, Any]:
        d["initial_state"] = _unjson(d.get("initial_state"), {})
        d["target_state"] = _unjson(d.get("target_state"), [])
        d["restrictions"] = _unjson(d.get("restrictions"), [])
        d["criteria"] = _unjson(d.get("criteria"), [])
        d["expected_actions"] = _unjson(d.get("expected_actions"), [])
        d["critical_errors"] = _unjson(d.get("critical_errors"), [])
        d["competency_codes"] = _unjson(d.get("competency_codes"), [])
        d["equipment_ids"] = _unjson(d.get("equipment_ids"), [])
        d["enabled"] = bool(d["enabled"])
        return d

    # ------------------------------------------------------------------
    # Scenarios
    # ------------------------------------------------------------------

    def upsert_scenario(self, module_id: int, w: ScenarioWrite) -> int:
        # Atomic INSERT ... ON CONFLICT(module_id) DO UPDATE ... RETURNING id;
        # see upsert_test for rationale. `module_id` is UNIQUE on
        # lms_scenarios in both _SCHEMA and _SCHEMA_POSTGRES. `status` is
        # intentionally left out of the SET clause (as in the old UPDATE
        # branch) so an existing scenario's status survives the upsert.
        with self._lock, self._conn:
            field_errors = (
                [f.model_dump() for f in w.field_errors]
                if w.multi_operator else []
            )
            values = (
                w.title, w.description, w.goal, _json(w.initial_state),
                _json([e.model_dump() for e in w.events]),
                _json([a.model_dump() for a in w.expected_actions]),
                _json([c.model_dump() for c in w.success_criteria]),
                _json([r.model_dump() for r in w.critical_errors]),
                _json([c.model_dump() for c in w.target_state]),
                _json(field_errors),
                _json(w.final_state), _json(w.competency_codes), _json(w.equipment_ids),
                w.duration_min, 1 if w.is_exam else 0, 1 if w.multi_operator else 0,
            )
            row = self._conn.execute(
                "INSERT INTO lms_scenarios (module_id, title, description, goal, initial_state, "
                "events, expected_actions, success_criteria, critical_errors, target_state, "
                "field_errors, final_state, competency_codes, equipment_ids, duration_min, "
                "is_exam, multi_operator, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(module_id) DO UPDATE SET "
                "title = excluded.title, description = excluded.description, goal = excluded.goal, "
                "initial_state = excluded.initial_state, events = excluded.events, "
                "expected_actions = excluded.expected_actions, "
                "success_criteria = excluded.success_criteria, "
                "critical_errors = excluded.critical_errors, target_state = excluded.target_state, "
                "field_errors = excluded.field_errors, final_state = excluded.final_state, "
                "competency_codes = excluded.competency_codes, equipment_ids = excluded.equipment_ids, "
                "duration_min = excluded.duration_min, is_exam = excluded.is_exam, "
                "multi_operator = excluded.multi_operator "
                "RETURNING id",
                (module_id,) + values + (time.time(),),
            ).fetchone()
            self._conn.commit()
            return int(row["id"])

    def delete_scenario(self, scenario_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM lms_scenarios WHERE id = ?", (scenario_id,))
            self._conn.commit()

    def get_scenario(self, scenario_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM lms_scenarios WHERE id = ?", (scenario_id,)
            ).fetchone()
        return self._decode_scenario(dict(row)) if row else None

    def get_scenario_by_module(self, module_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM lms_scenarios WHERE module_id = ?", (module_id,)
            ).fetchone()
        return self.get_scenario(int(row["id"])) if row else None

    def set_scenario_status(self, scenario_id: int, status: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE lms_scenarios SET status = ? WHERE id = ?", (status, scenario_id)
            )
            self._conn.commit()

    def list_scenarios(self) -> List[Dict[str, Any]]:
        """Все сценарии всех модулей с названиями модуля и курса."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.*, m.title AS module_title, m.course_id, "
                "c.title AS course_title "
                "FROM lms_scenarios s "
                "LEFT JOIN lms_course_modules m ON m.id = s.module_id "
                "LEFT JOIN lms_courses c ON c.id = m.course_id "
                "ORDER BY s.id"
            ).fetchall()
        return [self._decode_scenario(dict(r)) for r in rows]

    @staticmethod
    def _decode_scenario(d: Dict[str, Any]) -> Dict[str, Any]:
        d["initial_state"] = _unjson(d.get("initial_state"), {})
        d["events"] = _unjson(d.get("events"), [])
        d["expected_actions"] = _unjson(d.get("expected_actions"), [])
        d["success_criteria"] = _unjson(d.get("success_criteria"), [])
        d["critical_errors"] = _unjson(d.get("critical_errors"), [])
        d["target_state"] = _unjson(d.get("target_state"), [])
        d["field_errors"] = _unjson(d.get("field_errors"), [])
        d["final_state"] = _unjson(d.get("final_state"), {})
        d["competency_codes"] = _unjson(d.get("competency_codes"), [])
        d["equipment_ids"] = _unjson(d.get("equipment_ids"), [])
        d["is_exam"] = bool(d["is_exam"])
        d["multi_operator"] = bool(d.get("multi_operator"))
        return d

    # ------------------------------------------------------------------
    # Assessments
    # ------------------------------------------------------------------

    def create_assessment(self, a: Assessment) -> int:
        with self._lock, self._conn:
            new_id = self._conn.insert_returning_id(
                "INSERT INTO lms_assessments (user_id, module_id, kind, test_id, task_id, "
                "scenario_id, score, max_score, passed, criteria_scores, errors_count, "
                "critical_errors_count, duration_s, answers, feedback_good, feedback_bad, "
                "session_id, started_at, finished_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (a.user_id, a.module_id, a.kind.value, a.test_id, a.task_id, a.scenario_id,
                 a.score, a.max_score, 1 if a.passed else 0, _json(a.criteria_scores),
                 a.errors_count, a.critical_errors_count, a.duration_s, _json(a.answers),
                 _json(a.feedback_good), _json(a.feedback_bad), a.session_id,
                 a.started_at, a.finished_at, time.time()),
            )
            self._conn.commit()
            return new_id

    def get_assessment(self, assessment_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM lms_assessments WHERE id = ?", (assessment_id,)
            ).fetchone()
        return self._decode_assessment(dict(row)) if row else None

    def get_assessment_by_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM lms_assessments WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return self._decode_assessment(dict(row)) if row else None

    def list_assessments(self, user_id: Optional[int] = None,
                         module_id: Optional[int] = None, limit: int = 200) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM lms_assessments"
        conds: List[str] = []
        params: List[Any] = []
        if user_id is not None:
            conds.append("user_id = ?")
            params.append(user_id)
        if module_id is not None:
            conds.append("module_id = ?")
            params.append(module_id)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [self._decode_assessment(dict(r)) for r in rows]

    def latest_assessment(self, user_id: int, module_id: int, kind: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM lms_assessments WHERE user_id = ? AND module_id = ? AND kind = ? "
                "ORDER BY id DESC LIMIT 1",
                (user_id, module_id, kind),
            ).fetchone()
        return self._decode_assessment(dict(row)) if row else None

    @staticmethod
    def _decode_assessment(d: Dict[str, Any]) -> Dict[str, Any]:
        d["criteria_scores"] = _unjson(d.get("criteria_scores"), {})
        d["answers"] = _unjson(d.get("answers"))
        d["feedback_good"] = _unjson(d.get("feedback_good"), [])
        d["feedback_bad"] = _unjson(d.get("feedback_bad"), [])
        d["passed"] = bool(d["passed"])
        return d

    # ------------------------------------------------------------------
    # Action log
    # ------------------------------------------------------------------

    def add_action_log(self, entry: Dict[str, Any]) -> int:
        with self._lock, self._conn:
            new_id = self._conn.insert_returning_id(
                "INSERT INTO lms_action_log (timestamp, user_id, username, object_id, object_name, "
                "action, old_state, new_state, source, session_id, module_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (entry.get("timestamp", time.time()), entry.get("user_id"),
                 entry.get("username", ""), entry.get("object_id", ""),
                 entry.get("object_name", ""), entry.get("action", ""),
                 _json(entry.get("old_state")) or "{}", _json(entry.get("new_state")) or "{}",
                 entry.get("source", "operator_panel"), entry.get("session_id"),
                 entry.get("module_id")),
            )
            self._conn.commit()
            return new_id

    def add_action_logs(self, entries: List[Dict[str, Any]]) -> int:
        """Insert a complete session log in one transaction."""
        if not entries:
            return 0
        rows = [(
            entry.get("timestamp", time.time()), entry.get("user_id"),
            entry.get("username", ""), entry.get("object_id", ""),
            entry.get("object_name", ""), entry.get("action", ""),
            _json(entry.get("old_state")) or "{}", _json(entry.get("new_state")) or "{}",
            entry.get("source", "operator_panel"), entry.get("session_id"),
            entry.get("module_id"),
        ) for entry in entries]
        with self._lock, self._conn:
            # db.Connection has no `executemany` (neither sqlite3 nor psycopg
            # need it wrapped for this shim), so insert row-by-row inside a
            # single transaction instead.
            for params in rows:
                self._conn.execute(
                    "INSERT INTO lms_action_log (timestamp, user_id, username, object_id, object_name, "
                    "action, old_state, new_state, source, session_id, module_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", params,
                )
            self._conn.commit()
        return len(rows)

    def list_action_log(self, username: Optional[str] = None,
                        object_id: Optional[str] = None, session_id: Optional[str] = None,
                        limit: int = 500) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM lms_action_log"
        conds: List[str] = []
        params: List[Any] = []
        if username:
            conds.append("username = ?")
            params.append(username)
        if object_id:
            conds.append("object_id = ?")
            params.append(object_id)
        if session_id:
            conds.append("session_id = ?")
            params.append(session_id)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["old_state"] = _unjson(d.get("old_state"))
            d["new_state"] = _unjson(d.get("new_state"))
            out.append(d)
        return out

    # ------------------------------------------------------------------
    # SCADA interaction log (клики по объектам и время в окне)
    # ------------------------------------------------------------------

    def add_scada_log(self, entry: Dict[str, Any]) -> int:
        with self._lock, self._conn:
            new_id = self._conn.insert_returning_id(
                "INSERT INTO lms_scada_log (timestamp, user_id, username, event_type, "
                "object_id, object_name, duration_s, session_id, module_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (entry.get("timestamp", time.time()), entry.get("user_id"),
                 entry.get("username", ""), entry.get("event_type", "click"),
                 entry.get("object_id", ""), entry.get("object_name", ""),
                 entry.get("duration_s"), entry.get("session_id"), entry.get("module_id")),
            )
            self._conn.commit()
            return new_id

    def list_scada_log(self, username: Optional[str] = None,
                       object_id: Optional[str] = None,
                       event_type: Optional[str] = None,
                       session_id: Optional[str] = None,
                       limit: int = 500) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM lms_scada_log"
        conds: List[str] = []
        params: List[Any] = []
        if username:
            conds.append("username = ?")
            params.append(username)
        if object_id:
            conds.append("object_id = ?")
            params.append(object_id)
        if event_type:
            conds.append("event_type = ?")
            params.append(event_type)
        if session_id:
            conds.append("session_id = ?")
            params.append(session_id)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]
