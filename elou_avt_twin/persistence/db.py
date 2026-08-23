"""
db.py
=====
Thin DB-API compatibility layer so `SessionStore`, `AuthStore`, `LmsStore`
and `LmsContentStore` can keep their existing `?`-placeholder SQL and run
unmodified against either SQLite (local dev/tests) or PostgreSQL
(production, via `DATABASE_URL=postgresql://...`).

Design: the vast majority of the stores' SQL (SELECT/UPDATE/DELETE, and the
`ON CONFLICT(...) DO UPDATE SET x=excluded.x` upserts) is already portable
between SQLite and Postgres. Only three things differ and are handled here:

1. Placeholder style (`?` vs `%s`) -> `Connection.execute` rewrites `?` to
   `%s` for Postgres at call time (safe: none of the stores' SQL text
   contains a literal `?` character in string data).
2. `cursor.lastrowid` doesn't exist on Postgres -> use
   `Connection.insert_returning_id(sql, params)`, which appends
   `RETURNING id` on Postgres and falls back to `cursor.lastrowid` on
   SQLite.
3. `INSERT OR IGNORE` isn't valid Postgres syntax -> use
   `Connection.ignore_prefix()` / `Connection.ignore_suffix(cols)` to build
   the statement for either dialect from the same call site.

Schema DDL (`AUTOINCREMENT` vs `GENERATED ALWAYS AS IDENTITY`, `PRAGMA`
statements) is not portable and is kept as two explicit per-dialect
constants in each store module (`_SCHEMA_SQLITE` / `_SCHEMA_POSTGRES`).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Union

_QUESTION_MARK = re.compile(r"\?")


def json_dump(value: Any) -> Optional[str]:
    """Serialize a JSON-column value for storage; `None` stays `None`.

    Shared by the store modules that keep JSON-in-TEXT columns
    (`persistence.session_store`, `lms.store`, `lms.content_store`) so the
    same encoding/decoding rules apply everywhere instead of three
    independent copies.
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def json_load(text: Optional[str], default: Any = None) -> Any:
    """Deserialize a JSON-column value; tolerates `None`/malformed input."""
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def dialect_for(url: str) -> str:
    return "postgresql" if url.startswith("postgres") else "sqlite"


class Cursor:
    """Wraps a DB-API cursor; normalizes row access to plain dicts."""

    __slots__ = ("_cur", "dialect")

    def __init__(self, raw_cursor: Any, dialect: str):
        self._cur = raw_cursor
        self.dialect = dialect

    def fetchone(self) -> Optional[dict]:
        row = self._cur.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self) -> List[dict]:
        return [dict(r) for r in self._cur.fetchall()]

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def lastrowid(self) -> Optional[int]:
        # Only valid on SQLite; Postgres call sites must use
        # Connection.insert_returning_id() instead.
        return getattr(self._cur, "lastrowid", None)


class Connection:
    """DB-API-ish connection accepting `?`-style SQL regardless of backend."""

    def __init__(self, url: str):
        self.url = url
        self.dialect = dialect_for(url)
        if self.dialect == "sqlite":
            import sqlite3

            if url in ("sqlite://", "sqlite:///:memory:"):
                sqlite_path = ":memory:"
            elif url.startswith("sqlite:///"):
                sqlite_path = url[len("sqlite:///"):]
            else:
                sqlite_path = url
            self._raw = sqlite3.connect(sqlite_path, check_same_thread=False)
            self._raw.row_factory = sqlite3.Row
            self._raw.execute("PRAGMA journal_mode=WAL;")
            self._raw.execute("PRAGMA busy_timeout=5000;")
            self._raw.execute("PRAGMA foreign_keys=ON;")
        else:
            import psycopg
            from psycopg.rows import dict_row

            self._raw = psycopg.connect(url, row_factory=dict_row, autocommit=False)

    # -- statement execution -------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Cursor:
        if self.dialect == "postgresql":
            sql = _QUESTION_MARK.sub("%s", sql)
        cur = self._raw.cursor()
        cur.execute(sql, tuple(params))
        return Cursor(cur, self.dialect)

    def insert_returning_id(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Execute a single-row INSERT and return its new integer id,
        regardless of AUTOINCREMENT (SQLite) vs IDENTITY (Postgres)."""
        if self.dialect == "postgresql":
            sql = sql.rstrip().rstrip(";") + " RETURNING id"
            cur = self.execute(sql, params)
            row = cur.fetchone()
            return int(row["id"])
        cur = self.execute(sql, params)
        return int(cur.lastrowid)

    def ignore_prefix(self) -> str:
        """`INSERT [OR IGNORE] INTO` fragment for the current dialect."""
        return "INSERT OR IGNORE INTO" if self.dialect == "sqlite" else "INSERT INTO"

    def ignore_suffix(self, conflict_cols: str) -> str:
        """Trailing clause completing an ignore-duplicate insert."""
        if self.dialect == "sqlite":
            return ""
        return f" ON CONFLICT ({conflict_cols}) DO NOTHING"

    def executescript(self, script: str) -> None:
        if self.dialect == "sqlite":
            self._raw.executescript(script)
        else:
            with self._raw.cursor() as cur:
                for stmt in _split_statements(script):
                    cur.execute(stmt)

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


def _split_statements(script: str) -> Iterable[str]:
    for stmt in script.split(";"):
        stmt = stmt.strip()
        if stmt:
            yield stmt + ";"


def resolve_url(path_or_url: Optional[Union[str, Path]], default_sqlite_path: Path) -> str:
    """Resolve a store constructor's `path` argument (legacy filesystem-path
    contract, still used by tests) or fall back to `DATABASE_URL_FILE` /
    `DATABASE_URL` / the default local sqlite file.

    `DATABASE_URL_FILE` takes precedence when set: it points at a file (e.g.
    a Docker/Compose secret mounted at /run/secrets/database_url) whose
    content is the full connection string. This keeps the password out of
    the container's environment (and therefore out of `docker inspect`),
    unlike passing `DATABASE_URL` directly.
    """
    import os

    if path_or_url is not None:
        text = str(path_or_url)
        if text.startswith("postgres") or text.startswith("sqlite:"):
            return text
        if text == ":memory:":
            return "sqlite:///:memory:"
        return "sqlite:///" + text
    url_file = os.environ.get("DATABASE_URL_FILE", "").strip()
    if url_file:
        url = Path(url_file).read_text(encoding="utf-8").strip()
        if url:
            return url
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    default_sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    return "sqlite:///" + str(default_sqlite_path)


def connect(path_or_url: Optional[Union[str, Path]], default_sqlite_path: Path) -> Connection:
    return Connection(resolve_url(path_or_url, default_sqlite_path))
