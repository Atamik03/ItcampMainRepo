"""
store.py
========
SQLite RBAC store + service.

Table layout (created idempotently in the same `sessions.db` file used by
`persistence.session_store`):

    roles(code PK, name, description)
    permissions(code PK, description)
    role_permissions(role_code, permission_code)     -- many-to-many
    users(id PK, username UNIQUE, password_hash, full_name, is_active)
    user_roles(user_id, role_code)                   -- many-to-many

Authorization is always derived from permissions: a user's effective set is
the union of permissions of every role they belong to. The permission
catalog and role bindings below mirror the roles in `Роли.txt`.

Storage style follows `persistence.session_store` (WAL, busy_timeout,
foreign_keys, RLock) so the two stores can share one database file.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Union

from persistence import db as _db
from realtime import redis_bus

from .models import (
    LoginResponse,
    PermissionView,
    Principal,
    RoleCreate,
    RoleUpdate,
    RoleView,
    UserCreate,
    UserView,
)
from .security import create_token, hash_password, verify_password, verify_token

logger = logging.getLogger("elou_avt.auth")

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "sessions.db"
DEFAULT_SECRET_PATH = Path(__file__).resolve().parent.parent / ".auth_secret"


def _load_token_secret(explicit: Optional[str] = None) -> str:
    """Resolve the signing secret without shipping a universal demo secret.

    Production requires either ``ELOU_AUTH_SECRET`` or an explicitly-set
    ``ELOU_AUTH_SECRET_FILE`` pointing at a real secret (e.g. a mounted
    Docker/Compose secret file) -- both count as "a secret was actually
    supplied". Local MVP runs with neither set generate a strong
    per-installation secret once (written to the *default* secret file
    path), which keeps the one-click demo login working while preventing
    tokens from one copy of the project being valid in every other copy;
    that auto-generate fallback is deliberately refused in production.
    """
    secret = explicit or os.environ.get("ELOU_AUTH_SECRET", "").strip()
    environment = os.environ.get("ELOU_ENV", "development").strip().lower()
    if secret:
        if len(secret) < 32:
            raise RuntimeError("ELOU_AUTH_SECRET must contain at least 32 characters")
        return secret

    explicit_secret_file = os.environ.get("ELOU_AUTH_SECRET_FILE", "").strip()
    if explicit_secret_file:
        from_file = Path(explicit_secret_file).read_text(encoding="utf-8").strip()
        if len(from_file) < 32:
            raise RuntimeError(
                f"ELOU_AUTH_SECRET_FILE={explicit_secret_file} must contain at least 32 characters"
            )
        return from_file

    if environment in {"prod", "production"}:
        raise RuntimeError("ELOU_AUTH_SECRET or ELOU_AUTH_SECRET_FILE is required in production")

    path = Path(str(DEFAULT_SECRET_PATH))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if len(existing) >= 32:
            return existing
    except FileNotFoundError:
        pass

    generated = secrets.token_urlsafe(48)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(generated)
            stream.write("\n")
        return generated
    except FileExistsError:
        existing = path.read_text(encoding="utf-8").strip()
        if len(existing) < 32:
            raise RuntimeError(f"Token secret file {path} is invalid")
        return existing

# ---------------------------------------------------------------------------
# Permission catalog (single source of truth; "can_*" codes used everywhere).
# ---------------------------------------------------------------------------

PERMISSIONS: Dict[str, str] = {
    "view_scheme": "Просмотр мнемосхемы, телеметрии и алармов",
    "send_commands": "Управление оборудованием через HMI",
    "run_simulation": "Запуск и пошаговое управление симуляцией",
    "manage_twin": "Настройка цифрового двойника и параметров оборудования",
    "manage_alarms": "Настройка аварий и технологических ограничений",
    "manage_scheme": "Редактирование P&ID-схемы",
    "start_training": "Запуск и завершение тренировочных сессий",
    "view_training_sessions": "Просмотр журнала тренировок",
    "view_operator_actions": "Наблюдение за действиями оператора в реальном времени",
    "view_statistics": "Просмотр статистики",
    "manage_scenarios": "Создание и редактирование сценариев",
    "create_checklists": "Создание чек-листов",
    "create_grading_criteria": "Создание критериев оценки",
    "create_exam": "Создание экзаменов",
    "take_exam": "Прохождение экзаменов",
    "grade_exam": "Проверка экзаменов и утверждение квалификации",
    "view_exam_results": "Просмотр итоговых результатов экзаменов",
    "create_reports": "Формирование отчетов",
    "manage_users": "Управление пользователями",
    "manage_roles": "Управление ролями и правами",
    "view_logs": "Просмотр системных логов",
    "manage_reference_data": "Управление справочниками",
    "manage_equipment": "Управление оборудованием",
    "manage_ai": "Управление моделями и рекомендациями ИИ",
    "view_ai_analysis": "Просмотр статистики ошибок и анализа ИИ",
    "get_ai_recommendations": "Получение рекомендаций ИИ",
    "view_own_results": "Просмотр своих результатов",
    "view_dashboard": "Просмотр главной кабинета оператора",
    "view_field_operator_screen": "Просмотр 3D-экрана полевого оператора",
    "view_courses": "Просмотр курсов и модулей обучения",
    "view_competencies": "Просмотр карты компетенций",
    "view_history": "Просмотр истории тренировок",
    "view_profile": "Просмотр профиля пользователя",
    "manage_groups": "Управление учебными группами",
    "view_group_progress": "Просмотр прогресса учебных групп",
    "view_analytics": "Просмотр аналитики обучения",
    "monitor_operators": "Наблюдение за операторами в реальном времени",
    "manage_courses": "Управление курсами и модулями",
    "manage_practice_tasks": "Управление библиотекой практических заданий",
    "manage_settings": "Управление настройками системы",
}

ALL_PERMISSION_CODES: List[str] = sorted(PERMISSIONS)

ROLE_LABELS: Dict[str, str] = {
    "administrator": "Администратор",
    "instructor": "Инструктор",
    "operator": "Консольный оператор",
    "field_operator": "Полевой оператор",
}

ALL_ROLE_CODES: List[str] = list(ROLE_LABELS)

# Роли, удалённые из модели в ходе рефакторинга кабинетов: чистятся при
# каждом старте, чтобы не висели в базе. Пользовательские роли администратора
# (созданные через /auth/roles) НЕ трогаются.
LEGACY_ROLE_CODES: List[str] = ["methodologist", "process_engineer", "ai_analyst", "examiner"]

ROLE_DESCRIPTIONS: Dict[str, str] = {
    "administrator": "Полный доступ: пользователи, роли, система, логи, справочники",
    "instructor": "Все права, кроме управления пользователями и ролями (остаётся у администратора)",
    "operator": "Консольный оператор: управление процессом с HMI (SCADA), тренажер, свои результаты, рекомендации ИИ",
    "field_operator": "Полевой оператор: 3D-экран полевого оператора (компоновка установки и оборудования)",
}

ROLE_PERMISSIONS: Dict[str, List[str]] = {
    "administrator": ALL_PERMISSION_CODES,
    # Все права, кроме управления учётками (пользователи и роли/права) —
    # это остаётся за администратором.
    "instructor": [
        c for c in ALL_PERMISSION_CODES if c not in ("manage_users", "manage_roles")
    ],
    # start_training включено: оператор сам открывает тренировку в демо-HMI
    # («▶ Запустить сценарий»), хотя в промышленной модели запуск за инструктором.
    "operator": [
        "view_scheme", "send_commands", "run_simulation", "start_training",
        "take_exam", "view_own_results", "get_ai_recommendations",
        "view_dashboard", "view_courses", "view_competencies",
        "view_history", "view_profile",
    ],
    # Полевой оператор: полный кабинет консольного оператора БЕЗ SCADA
    # (мнемосхема/HMI: view_scheme, send_commands, run_simulation). Вместо
    # SCADA — 3D-экран полевого оператора (компоновка установки).
    "field_operator": [
        "start_training", "take_exam", "view_own_results", "get_ai_recommendations",
        "view_dashboard", "view_courses", "view_competencies",
        "view_history", "view_profile", "view_field_operator_screen",
    ],
}

# (username, password, full_name, [role_codes]) — seed accounts for the demo.
# Пароли равны логину; в проде заменяются администратором.
DEFAULT_USERS = [
    ("admin", "admin", "Системный администратор", ["administrator"]),
    ("instructor", "instructor", "Инструктор", ["instructor"]),
    ("operator", "operator", "Консольный оператор", ["operator"]),
    ("field_operator", "field_operator", "Полевой оператор", ["field_operator"]),
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS roles (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS permissions (
    code        TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS role_permissions (
    role_code       TEXT NOT NULL REFERENCES roles(code) ON DELETE CASCADE,
    permission_code TEXT NOT NULL REFERENCES permissions(code) ON DELETE CASCADE,
    PRIMARY KEY (role_code, permission_code)
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    full_name     TEXT NOT NULL DEFAULT '',
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS user_roles (
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_code TEXT NOT NULL REFERENCES roles(code) ON DELETE CASCADE,
    PRIMARY KEY (user_id, role_code)
);
"""

_SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS roles (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS permissions (
    code        TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS role_permissions (
    role_code       TEXT NOT NULL REFERENCES roles(code) ON DELETE CASCADE,
    permission_code TEXT NOT NULL REFERENCES permissions(code) ON DELETE CASCADE,
    PRIMARY KEY (role_code, permission_code)
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    full_name     TEXT NOT NULL DEFAULT '',
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS user_roles (
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_code TEXT NOT NULL REFERENCES roles(code) ON DELETE CASCADE,
    PRIMARY KEY (user_id, role_code)
);
"""


class AuthStore:
    """SQLite-backed RBAC store (roles, permissions, users, bindings)."""

    def __init__(self, path: Optional[Union[Path, str]] = None):
        self._path = Path(path) if path else DEFAULT_DB_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = _db.connect(path, DEFAULT_DB_PATH)
        with self._lock:
            self._conn.executescript(_SCHEMA if self._conn.dialect == "sqlite" else _SCHEMA_POSTGRES)
            self._conn.commit()
        logger.info("AuthStore opened: %s", self._path)

    @classmethod
    def in_memory(cls) -> "AuthStore":
        return cls(path=":memory:")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # Seeds
    # ------------------------------------------------------------------

    def ensure_catalog(self) -> None:
        """Idempotently insert the permission catalog and role bindings."""
        with self._lock, self._conn:
            for code, desc in PERMISSIONS.items():
                self._conn.execute(
                    f"{self._conn.ignore_prefix()} permissions (code, description) "
                    f"VALUES (?, ?){self._conn.ignore_suffix('code')}",
                    (code, desc),
                )
            # Устаревшие роли (методолог, технолог, аналитик ИИ, экзаменатор)
            # удаляются — остаются только кабинеты. Пользовательские роли,
            # созданные администратором, не удаляются. Связи
            # role_permissions/user_roles слетают по CASCADE.
            self._conn.execute(
                # The interpolated part is only a "?,?,?" placeholder-count
                # string sized off len(LEGACY_ROLE_CODES), a fixed
                # module-level constant list; every actual value is still
                # bound as a real ? parameter below, not interpolated.
                "DELETE FROM roles WHERE code IN (%s)"  # nosec B608
                % ",".join("?" * len(LEGACY_ROLE_CODES)),
                LEGACY_ROLE_CODES,
            )
            # Вместе с ролями удаляем не используемые больше демо-учетки.
            self._conn.execute(
                "DELETE FROM users WHERE username IN "
                "('methodologist','process_engineer','ai_analyst','examiner')",
            )
            for code, name in ROLE_LABELS.items():
                self._conn.execute(
                    f"{self._conn.ignore_prefix()} roles (code, name, description) "
                    f"VALUES (?, ?, ?){self._conn.ignore_suffix('code')}",
                    (code, name, ROLE_DESCRIPTIONS.get(code, "")),
                )
            for role, codes in ROLE_PERMISSIONS.items():
                # Каталог является источником истины для ВСТРОЕННЫХ ролей:
                # права заменяются на каждом старте, чтобы изменения каталога
                # (новые роли/права) применялись к существующим базам.
                # Пользовательские роли сюда не входят и не трогаются.
                self._conn.execute(
                    "DELETE FROM role_permissions WHERE role_code = ?", (role,)
                )
                for perm in codes:
                    self._conn.execute(
                        f"{self._conn.ignore_prefix()} role_permissions (role_code, permission_code) "
                        f"VALUES (?, ?){self._conn.ignore_suffix('role_code, permission_code')}",
                        (role, perm),
                    )
            self._conn.commit()

    def ensure_default_users(self) -> None:
        """Seed demo accounts idempotently: add missing users, repair role bindings."""
        self.ensure_catalog()
        with self._lock, self._conn:
            for username, password, full_name, roles in DEFAULT_USERS:
                row = self._conn.execute(
                    "SELECT id FROM users WHERE username = ?", (username,)
                ).fetchone()
                if row is None:
                    now = time.time()
                    user_id = self._conn.insert_returning_id(
                        "INSERT INTO users (username, password_hash, full_name, is_active, created_at) "
                        "VALUES (?, ?, ?, 1, ?)",
                        (username, hash_password(password), full_name, now),
                    )
                else:
                    user_id = row["id"]
                    self._conn.execute(
                        "UPDATE users SET full_name = ?, is_active = 1 WHERE id = ?",
                        (full_name, user_id),
                    )
                for role in roles:
                    self._conn.execute(
                        f"{self._conn.ignore_prefix()} user_roles (user_id, role_code) "
                        f"VALUES (?, ?){self._conn.ignore_suffix('user_id, role_code')}",
                        (user_id, role),
                    )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def get_user(self, user_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_user_by_name(self, username: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
            return dict(row) if row else None

    def create_user(self, data: UserCreate) -> UserView:
        with self._lock, self._conn:
            now = time.time()
            user_id = self._conn.insert_returning_id(
                "INSERT INTO users (username, password_hash, full_name, is_active, created_at) "
                "VALUES (?, ?, ?, 1, ?)",
                (data.username, hash_password(data.password), data.full_name, now),
            )
            self._bind_roles_locked(user_id, data.role_codes)
            self._conn.commit()
        return self.user_view(user_id)

    def set_user_active(self, user_id: int, is_active: bool) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE users SET is_active = ? WHERE id = ?",
                (1 if is_active else 0, user_id),
            )
            self._conn.commit()
        user = self.get_user(user_id)
        if user is not None:
            redis_bus.invalidate_principal_cache(user["username"])

    def set_user_roles(self, user_id: int, role_codes: List[str]) -> UserView:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM user_roles WHERE user_id = ?", (user_id,))
            self._bind_roles_locked(user_id, role_codes)
            self._conn.commit()
        user = self.get_user(user_id)
        if user is not None:
            redis_bus.invalidate_principal_cache(user["username"])
        return self.user_view(user_id)

    def _bind_roles_locked(self, user_id: int, role_codes: List[str]) -> None:
        """Insert role bindings for roles that actually exist (incl. custom)."""
        known = {
            r["code"]
            for r in self._conn.execute("SELECT code FROM roles").fetchall()
        }
        for role in role_codes:
            if role in known:
                self._conn.execute(
                    f"{self._conn.ignore_prefix()} user_roles (user_id, role_code) "
                    f"VALUES (?, ?){self._conn.ignore_suffix('user_id, role_code')}",
                    (user_id, role),
                )

    def list_users(self) -> List[UserView]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM users ORDER BY id"
            ).fetchall()
        return [self.user_view(int(r["id"])) for r in rows]

    def list_roles(self) -> List[RoleView]:
        self.ensure_catalog()
        out: List[RoleView] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT code, name, description FROM roles ORDER BY code"
            ).fetchall()
            for r in rows:
                perms = [p["permission_code"] for p in self._conn.execute(
                    "SELECT permission_code FROM role_permissions WHERE role_code = ? ORDER BY permission_code",
                    (r["code"],),
                ).fetchall()]
                out.append(RoleView(
                    code=r["code"], name=r["name"], description=r["description"], permissions=perms,
                ))
        return out

    def all_permissions(self) -> List[PermissionView]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT code, description FROM permissions ORDER BY code"
            ).fetchall()
        return [PermissionView(code=r["code"], description=r["description"]) for r in rows]

    # ------------------------------------------------------------------
    # Roles (администрирование)
    # ------------------------------------------------------------------

    _ROLE_CODE_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")

    def _known_permission_codes(self) -> Set[str]:
        return {
            r["code"]
            for r in self._conn.execute("SELECT code FROM permissions").fetchall()
        }

    def create_role(self, data: RoleCreate) -> RoleView:
        code = data.code.strip()
        if not self._ROLE_CODE_RE.fullmatch(code):
            raise ValueError("Код роли: латиница, цифры и '_' (до 64 символов)")
        with self._lock, self._conn:
            if self._conn.execute(
                "SELECT 1 FROM roles WHERE code = ?", (code,)
            ).fetchone():
                raise ValueError(f"Роль '{code}' уже существует")
            self._conn.execute(
                "INSERT INTO roles (code, name, description) VALUES (?, ?, ?)",
                (code, data.name.strip(), data.description.strip()),
            )
            self._bind_permissions_locked(code, data.permission_codes)
            self._conn.commit()
        return self.role_view(code)

    def update_role(self, code: str, data: RoleUpdate) -> RoleView:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT code FROM roles WHERE code = ?", (code,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Роль '{code}' не найдена")
            if data.name is not None:
                self._conn.execute(
                    "UPDATE roles SET name = ? WHERE code = ?",
                    (data.name.strip(), code),
                )
            if data.description is not None:
                self._conn.execute(
                    "UPDATE roles SET description = ? WHERE code = ?",
                    (data.description.strip(), code),
                )
            self._conn.commit()
        return self.role_view(code)

    def set_role_permissions(self, code: str, permission_codes: List[str]) -> RoleView:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT code FROM roles WHERE code = ?", (code,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Роль '{code}' не найдена")
            self._conn.execute(
                "DELETE FROM role_permissions WHERE role_code = ?", (code,)
            )
            self._bind_permissions_locked(code, permission_codes)
            self._conn.commit()
        # A role can be held by many users and the Principal cache is keyed
        # by token hash (not username), so precise invalidation here would
        # require a role -> users reverse index. Skip it: cached Principals
        # naturally expire within _PRINCIPAL_CACHE_TTL_SECONDS (5 min, see
        # auth/deps.py), which is the accepted staleness bound for this case.
        return self.role_view(code)

    def delete_role(self, code: str) -> None:
        if code in ROLE_LABELS:
            raise ValueError(
                f"Встроенную роль '{code}' нельзя удалить — она восстанавливается каталогом"
            )
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM roles WHERE code = ?", (code,))
            if cur.rowcount == 0:
                raise KeyError(f"Роль '{code}' не найдена")
            self._conn.commit()

    def _bind_permissions_locked(self, role_code: str, permission_codes: List[str]) -> None:
        known = self._known_permission_codes()
        for perm in permission_codes:
            if perm in known:
                self._conn.execute(
                    f"{self._conn.ignore_prefix()} role_permissions (role_code, permission_code) "
                    f"VALUES (?, ?){self._conn.ignore_suffix('role_code, permission_code')}",
                    (role_code, perm),
                )

    def role_view(self, code: str) -> RoleView:
        with self._lock:
            row = self._conn.execute(
                "SELECT code, name, description FROM roles WHERE code = ?", (code,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Роль '{code}' не найдена")
            perms = [p["permission_code"] for p in self._conn.execute(
                "SELECT permission_code FROM role_permissions WHERE role_code = ? ORDER BY permission_code",
                (code,),
            ).fetchall()]
            return RoleView(
                code=row["code"], name=row["name"], description=row["description"], permissions=perms,
            )

    # ------------------------------------------------------------------
    # Queries used to build a Principal
    # ------------------------------------------------------------------

    def roles_for_user(self, username: str) -> List[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT r.code FROM users u "
                "JOIN user_roles ur ON ur.user_id = u.id "
                "JOIN roles r ON r.code = ur.role_code "
                "WHERE u.username = ? ORDER BY r.code",
                (username,),
            ).fetchall()
        return [r["code"] for r in rows]

    def permissions_for_user(self, username: str) -> List[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT rp.permission_code FROM users u "
                "JOIN user_roles ur ON ur.user_id = u.id "
                "JOIN role_permissions rp ON rp.role_code = ur.role_code "
                "WHERE u.username = ? ORDER BY rp.permission_code",
                (username,),
            ).fetchall()
        return [r["permission_code"] for r in rows]

    def user_view(self, user_id: int) -> UserView:
        row = self.get_user(user_id)
        if row is None:
            raise ValueError(f"User {user_id} not found")
        username = row["username"]
        return UserView(
            id=user_id,
            username=username,
            full_name=row.get("full_name", ""),
            is_active=bool(row.get("is_active")),
            roles=self.roles_for_user(username),
            permissions=self.permissions_for_user(username),
        )

    def principal_for(self, username: str) -> Optional[Principal]:
        user = self.get_user_by_name(username)
        if user is None or not user.get("is_active"):
            return None
        return Principal(
            username=username,
            full_name=user.get("full_name", ""),
            roles=self.roles_for_user(username),
            permissions=self.permissions_for_user(username),
        )


class AuthService:
    """High-level RBAC service: authentication, token issuing and user admin."""

    def __init__(
        self,
        store: Optional[AuthStore] = None,
        secret: Optional[str] = None,
        token_ttl: int = 8 * 3600,
    ):
        self._store = store or AuthStore()
        self._secret = _load_token_secret(secret)
        self._ttl = token_ttl
        self._store.ensure_catalog()
        self._store.ensure_default_users()

    @property
    def store(self) -> AuthStore:
        return self._store

    def authenticate(self, username: str, password: str) -> Optional[LoginResponse]:
        user = self._store.get_user_by_name(username)
        if user is None or not user.get("is_active"):
            return None
        if not verify_password(password, user["password_hash"]):
            return None
        token = create_token(self._secret, username, ttl_seconds=self._ttl)
        principal = self._store.principal_for(username)
        return LoginResponse(
            access_token=token,
            expires_in=self._ttl,
            user=UserView(
                id=user["id"], username=username,
                full_name=user.get("full_name", ""),
                is_active=True,
                roles=principal.roles if principal else [],
                permissions=principal.permissions if principal else [],
            ),
        )

    def principal_from_token(self, token: str) -> Optional[Principal]:
        payload = verify_token(self._secret, token)
        if payload is None:
            return None
        return self._store.principal_for(payload.get("sub", ""))

    def principal_for(self, username: str) -> Optional[Principal]:
        return self._store.principal_for(username)

    def create_user(self, data: UserCreate) -> UserView:
        return self._store.create_user(data)

    def set_user_active(self, user_id: int, is_active: bool) -> None:
        self._store.set_user_active(user_id, is_active)

    def set_user_roles(self, user_id: int, role_codes: List[str]) -> UserView:
        return self._store.set_user_roles(user_id, role_codes)

    def list_users(self) -> List[UserView]:
        return self._store.list_users()

    def list_roles(self) -> List[RoleView]:
        return self._store.list_roles()

    def all_permissions(self) -> List[PermissionView]:
        return self._store.all_permissions()

    def create_role(self, data: RoleCreate) -> RoleView:
        return self._store.create_role(data)

    def update_role(self, code: str, data: RoleUpdate) -> RoleView:
        return self._store.update_role(code, data)

    def set_role_permissions(self, code: str, permission_codes: List[str]) -> RoleView:
        return self._store.set_role_permissions(code, permission_codes)

    def delete_role(self, code: str) -> None:
        self._store.delete_role(code)
