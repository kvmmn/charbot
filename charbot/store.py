"""SQLite or Postgres persistence for identity, work, comms, and ops."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from charbot.members import BOARD_MEMBERS, MEMBER_BY_KEY

SCHEMA_SQL_PATH = Path(__file__).resolve().parent.parent / "schema.sql"
ORG_SLUG = "chaharsotoon"
ORG_NAME = "\u0686\u0647\u0627\u0631\u0633\u062a\u0648\u0646"
KNOWN_GROUP_CHAT_ID = -1002781646107
KNOWN_GROUP_TITLE = "X-Chaharsotoon"
SHEY_SLUG = "shey"
SHEY_NAME = "SHEY"
HAMED_ROLE_SUMMARY = "مدیرعامل، سرپرست طراحی، طراح"
KAWE_ROLE_SUMMARY = (
    "رئیس هیئت مدیره؛ remote برلین؛ هماهنگی، مشاوره، AM/client، PMO، tech/AI"
)
GHAZAL_NOTE = "کارمند بازاریابی، برندینگ، طراحی و اجرا"
SEARCH_PATH = "identity, work, comms, ops, public"
SEARCH_PATH_OPTIONS = "identity,work,comms,ops,public"


class TaskStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"


@dataclass
class Task:
    id: int
    group_id: int
    title: str
    description: str | None
    assignee_key: str | None
    due_date: date | None
    status: TaskStatus
    created_by_user_id: int | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


@dataclass
class TelegramUserMapping:
    telegram_user_id: int
    member_key: str
    username: str | None
    display_name: str | None


STAFF_KEYS = frozenset({"ghazal"})

SEEDED_FACTS: tuple[tuple[str, str, str, str, str], ...] = (
    ("kawe", "role", "summary", KAWE_ROLE_SUMMARY, "seed"),
    ("hamed", "role", "summary", HAMED_ROLE_SUMMARY, "seed"),
    ("ghazal", "note", "summary", GHAZAL_NOTE, "seed"),
)

TASK_SELECT = """
SELECT
  t.id,
  t.telegram_chat_id AS group_id,
  t.title,
  t.description,
  (
    SELECT p.slug FROM task_assignees a
    JOIN people p ON p.id = a.person_id
    WHERE a.task_id = t.id
    ORDER BY a.assigned_at ASC, p.slug ASC
    LIMIT 1
  ) AS assignee_key,
  t.due_date,
  t.status,
  (
    SELECT pi.provider_user_id FROM person_identities pi
    WHERE pi.person_id = t.created_by_person_id AND pi.provider = 'telegram'
    LIMIT 1
  ) AS created_by_user_id,
  t.created_at,
  t.updated_at,
  t.completed_at
FROM tasks t
"""

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS organizations (
    id TEXT PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS groups (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    telegram_chat_id INTEGER UNIQUE,
    title TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS people (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    display_name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('board','staff','contractor','client')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (organization_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_people_org_kind ON people(organization_id, kind);

CREATE TABLE IF NOT EXISTS person_identities (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('telegram','email','github')),
    provider_user_id TEXT NOT NULL,
    username TEXT,
    display_name TEXT,
    is_primary INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    UNIQUE (provider, provider_user_id),
    UNIQUE (person_id, provider)
);

CREATE TABLE IF NOT EXISTS person_roles (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    department TEXT,
    is_primary INTEGER NOT NULL DEFAULT 0,
    source TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS person_memories (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (person_id, kind, key)
);
CREATE INDEX IF NOT EXISTS idx_person_memories_person_kind ON person_memories(person_id, kind);

CREATE TABLE IF NOT EXISTS person_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload TEXT,
    telegram_message_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    UNIQUE (organization_id, slug)
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    group_id TEXT REFERENCES groups(id),
    project_id TEXT REFERENCES projects(id),
    telegram_chat_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','in_progress','done','cancelled')),
    due_date TEXT,
    created_by_person_id TEXT REFERENCES people(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_chat_status ON tasks(telegram_chat_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_due_date ON tasks(due_date);
CREATE INDEX IF NOT EXISTS idx_tasks_completed_at ON tasks(completed_at);
CREATE INDEX IF NOT EXISTS idx_tasks_status_completed ON tasks(status, completed_at);

CREATE TABLE IF NOT EXISTS task_assignees (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    person_id TEXT NOT NULL REFERENCES people(id),
    assigned_at TEXT NOT NULL,
    PRIMARY KEY (task_id, person_id)
);
CREATE INDEX IF NOT EXISTS idx_task_assignees_person ON task_assignees(person_id);
CREATE INDEX IF NOT EXISTS idx_task_assignees_person_task ON task_assignees(person_id, task_id);

CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload TEXT,
    actor_person_id TEXT REFERENCES people(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT REFERENCES groups(id),
    person_id TEXT REFERENCES people(id),
    telegram_chat_id INTEGER NOT NULL,
    telegram_message_id INTEGER,
    telegram_update_id INTEGER UNIQUE,
    direction TEXT NOT NULL CHECK (direction IN ('in','out')),
    kind TEXT NOT NULL,
    body TEXT,
    processed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_created ON messages(telegram_chat_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_person ON messages(person_id);

CREATE TABLE IF NOT EXISTS message_media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    file_id TEXT,
    storage_path TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lessons (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    source_person_id TEXT REFERENCES people(id),
    created_at TEXT NOT NULL,
    UNIQUE (organization_id, body)
);

CREATE TABLE IF NOT EXISTS settings (
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    group_id TEXT REFERENCES groups(id),
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_settings_scope
    ON settings (organization_id, ifnull(group_id, ''), key);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""

def _utcnow() -> datetime:
    return datetime.now(UTC)


def _dt_to_str(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _new_id() -> str:
    return str(uuid4())


def _as_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    return datetime.fromisoformat(str(value))


def _as_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def parse_role_titles(value: str) -> list[str]:
    text = (value or "").strip()
    if not text:
        return []
    # Semicolon summaries (Kawe) keep a single title before the first ؛.
    if "\u061b" in text:
        head = text.split("\u061b")[0].strip()
        return [head] if head else [text]
    if "\u060c" in text:
        return [part.strip() for part in text.split("\u060c") if part.strip()]
    return [text]


def _looks_like_shey(title: str | None, description: str | None) -> bool:
    blob = f"{title or ''} {description or ''}"
    return "\u0634\u06cc" in blob or "shey" in blob.lower()


def _row_to_task(row: Any) -> Task:
    return Task(
        id=int(row["id"]),
        group_id=int(row["group_id"]),
        title=row["title"],
        description=row["description"],
        assignee_key=row["assignee_key"],
        due_date=_as_date(row["due_date"]),
        status=TaskStatus(row["status"]),
        created_by_user_id=_as_int(row["created_by_user_id"]),
        created_at=_as_dt(row["created_at"]),
        updated_at=_as_dt(row["updated_at"]),
        completed_at=_as_dt(row["completed_at"]) if row["completed_at"] else None,
    )


def _table_columns_sqlite(conn: Any, table: str) -> set[str]:
    return {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})")}


def _split_sql(sql: str) -> list[str]:
    statements: list[str] = []
    buf: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.endswith(";"):
            stmt = "\n".join(buf).strip().rstrip(";").strip()
            if stmt:
                statements.append(stmt)
            buf = []
    tail = "\n".join(buf).strip().rstrip(";").strip()
    if tail:
        statements.append(tail)
    return statements


def _load_pg_sql() -> tuple[str, str]:
    text = SCHEMA_SQL_PATH.read_text(encoding="utf-8")
    if "-- COMPAT VIEWS" in text:
        ddl, views = text.split("-- COMPAT VIEWS", 1)
        return ddl, views
    return text, ""


class _AdaptConn:
    """Thin execute wrapper: sqlite keeps `?`; postgres gets `%s`."""

    def __init__(self, conn: Any, *, postgres: bool) -> None:
        self._conn = conn
        self._postgres = postgres

    def _sql(self, sql: str) -> str:
        if self._postgres:
            return sql.replace("%", "%%").replace("?", "%s")
        return sql

    def execute(self, sql: str, params: Any = None) -> Any:
        sql = self._sql(sql)
        if params is None:
            return self._conn.execute(sql)
        return self._conn.execute(sql, params)

    def executescript(self, sql: str) -> None:
        if not self._postgres:
            self._conn.executescript(sql)
            return
        for stmt in _split_sql(sql):
            self._conn.execute(stmt)


def store_from_settings(settings: Any) -> TaskStore:
    database_url = str(getattr(settings, "database_url", "") or "").strip()
    database_path = Path(settings.database_path)
    if database_url:
        return TaskStore(db_path=database_path, dsn=database_url)
    return TaskStore(database_path)

class TaskStore:
    def __init__(self, db_path: Path, dsn: str | None = None) -> None:
        self.db_path = Path(db_path)
        self._dsn = (dsn or "").strip() or None
        self._org_id: str | None = None
        if self._dsn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _ddl_dsn(self) -> str:
        assert self._dsn
        direct = os.environ.get("DATABASE_URL_DIRECT", "").strip()
        return direct or self._dsn

    def _connect_pg(self, dsn: str) -> Any:
        raw = psycopg.connect(
            dsn,
            row_factory=dict_row,
            connect_timeout=15,
        )
        # Neon pooler rejects search_path as a startup parameter; SET after connect.
        raw.execute(f"SET search_path TO {SEARCH_PATH}")
        return raw

    @contextmanager
    def _conn(self) -> Iterator[_AdaptConn]:
        if self._dsn:
            raw = self._connect_pg(self._dsn)
            wrapped = _AdaptConn(raw, postgres=True)
            try:
                yield wrapped
                raw.commit()
            except Exception:
                raw.rollback()
                raise
            finally:
                raw.close()
            return
        raw_sqlite = sqlite3.connect(self.db_path)
        raw_sqlite.row_factory = sqlite3.Row
        raw_sqlite.execute("PRAGMA journal_mode=WAL")
        raw_sqlite.execute("PRAGMA foreign_keys=ON")
        raw_sqlite.execute("PRAGMA busy_timeout=5000")
        wrapped = _AdaptConn(raw_sqlite, postgres=False)
        try:
            yield wrapped
            raw_sqlite.commit()
        finally:
            raw_sqlite.close()

    def _init_db(self) -> None:
        if self._dsn:
            raw = self._connect_pg(self._ddl_dsn())
            conn = _AdaptConn(raw, postgres=True)
            try:
                self._init_postgres(conn)
                raw.commit()
            except Exception:
                raw.rollback()
                raise
            finally:
                raw.close()
            return
        with self._conn() as conn:
            self._init_sqlite(conn)

    def _init_sqlite(self, conn: _AdaptConn) -> None:
        self._migrate_sqlite_legacy_rename(conn)
        conn.executescript(_SQLITE_SCHEMA)
        self._migrate_sqlite_kv(conn)
        self._seed_all(conn)
        conn.execute(
            """
            INSERT INTO schema_migrations (version, name, applied_at)
            VALUES (?, ?, ?)
            ON CONFLICT (version) DO NOTHING
            """,
            (2, "normalize_identity_work_comms_ops", _dt_to_str(_utcnow())),
        )

    def _init_postgres(self, conn: _AdaptConn) -> None:
        ddl, views = _load_pg_sql()
        conn.executescript(ddl)
        if self._public_relkind(conn, "people") == "r":
            self._migrate_postgres_legacy(conn)
            self._verify_postgres_migration(conn)
            self._drop_postgres_legacy(conn)
            if views.strip():
                conn.executescript(views)
        elif views.strip():
            conn.executescript(views)
        self._seed_all(conn)
        conn.execute(
            """
            INSERT INTO schema_migrations (version, name, applied_at)
            VALUES (?, ?, ?)
            ON CONFLICT (version) DO NOTHING
            """,
            (2, "normalize_identity_work_comms_ops", _dt_to_str(_utcnow())),
        )

    def _public_relkind(self, conn: _AdaptConn, name: str) -> str | None:
        row = conn.execute(
            """
            SELECT c.relkind AS relkind
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = ?
            """,
            (name,),
        ).fetchone()
        return None if not row else row["relkind"]

    def _migrate_sqlite_legacy_rename(self, conn: _AdaptConn) -> None:
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

        def rename(table: str, required: str | None = None) -> None:
            if table not in tables:
                return
            if required:
                cols = _table_columns_sqlite(conn, table)
                if required in cols:
                    return
            dest = f"{table}_legacy"
            n = 1
            existing = {
                r["name"]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            while dest in existing:
                n += 1
                dest = f"{table}_legacy{n}"
            conn.execute(f'ALTER TABLE "{table}" RENAME TO "{dest}"')
            tables.discard(table)
            tables.add(dest)

        rename("people", "slug")
        rename("tasks", "telegram_chat_id")
        rename("lessons", "body")
        rename("person_events", "person_id")
        for table in ("user_mappings", "inbox", "conversation", "person_facts"):
            rename(table)

    def _migrate_sqlite_kv(self, conn: _AdaptConn) -> None:
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "kv" not in tables:
            return
        org_id = self._ensure_org(conn)
        now = _dt_to_str(_utcnow())
        for row in conn.execute("SELECT key, value, updated_at FROM kv"):
            key = str(row["key"])
            value = row["value"]
            updated = row["updated_at"] or now
            if key.startswith("role_"):
                member_key = key[len("role_") :]
                if not member_key:
                    continue
                person_id = self._ensure_person(conn, member_key)
                conn.execute(
                    """
                    INSERT INTO person_memories (
                        id, person_id, kind, key, value, source, created_at, updated_at
                    ) VALUES (?, ?, 'role', 'summary', ?, 'kv', ?, ?)
                    ON CONFLICT (person_id, kind, key) DO NOTHING
                    """,
                    (_new_id(), person_id, value, updated, updated),
                )
                self._ensure_roles_from_summary(conn, person_id, value, "kv")
                continue
            conn.execute(
                """
                INSERT INTO settings (organization_id, group_id, key, value, updated_at)
                SELECT ?, NULL, ?, ?, ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM settings
                    WHERE organization_id = ? AND key = ? AND group_id IS NULL
                )
                """,
                (org_id, key, json.dumps(value, ensure_ascii=False), updated, org_id, key),
            )

    def _migrate_postgres_legacy(self, conn: _AdaptConn) -> None:
        conn.execute(
            """
            INSERT INTO identity.organizations (slug, name)
            VALUES (?, ?)
            ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
            """,
            (ORG_SLUG, ORG_NAME),
        )
        conn.execute(
            """
            INSERT INTO identity.groups (organization_id, telegram_chat_id, title)
            SELECT o.id, ?, ?
            FROM identity.organizations o
            WHERE o.slug = ?
            ON CONFLICT (telegram_chat_id) DO UPDATE SET title = EXCLUDED.title
            """,
            (KNOWN_GROUP_CHAT_ID, KNOWN_GROUP_TITLE, ORG_SLUG),
        )
        conn.execute(
            """
            INSERT INTO work.projects (organization_id, slug, name, status)
            SELECT o.id, ?, ?, 'active'
            FROM identity.organizations o
            WHERE o.slug = ?
            ON CONFLICT (organization_id, slug) DO NOTHING
            """,
            (SHEY_SLUG, SHEY_NAME, ORG_SLUG),
        )
        conn.execute(
            """
            INSERT INTO identity.people (
                organization_id, slug, display_name, kind, status, created_at, updated_at
            )
            SELECT o.id, p.member_key, p.display_name, p.kind, 'active',
                   p.created_at::timestamptz, p.updated_at::timestamptz
            FROM public.people p
            JOIN identity.organizations o ON o.slug = ?
            ON CONFLICT (organization_id, slug) DO NOTHING
            """,
            (ORG_SLUG,),
        )
        conn.execute(
            """
            INSERT INTO identity.person_identities (
                person_id, provider, provider_user_id, username, display_name,
                is_primary, updated_at
            )
            SELECT p.id, 'telegram', um.telegram_user_id::text, um.username,
                   um.display_name, true, COALESCE(um.updated_at::timestamptz, now())
            FROM public.user_mappings um
            JOIN identity.people p ON p.slug = um.member_key
            JOIN identity.organizations o ON o.id = p.organization_id AND o.slug = ?
            ON CONFLICT (provider, provider_user_id) DO UPDATE SET
                person_id = EXCLUDED.person_id,
                username = COALESCE(
                    EXCLUDED.username, identity.person_identities.username
                ),
                display_name = COALESCE(
                    EXCLUDED.display_name, identity.person_identities.display_name
                ),
                updated_at = EXCLUDED.updated_at
            """,
            (ORG_SLUG,),
        )
        conn.execute(
            """
            INSERT INTO identity.person_identities (
                person_id, provider, provider_user_id, username, display_name,
                is_primary, updated_at
            )
            SELECT p.id, 'telegram', op.telegram_user_id::text, op.username,
                   op.display_name, true, op.updated_at::timestamptz
            FROM public.people op
            JOIN identity.people p ON p.slug = op.member_key
            JOIN identity.organizations o ON o.id = p.organization_id AND o.slug = ?
            WHERE op.telegram_user_id IS NOT NULL
            ON CONFLICT (provider, provider_user_id) DO UPDATE SET
                username = COALESCE(EXCLUDED.username, identity.person_identities.username),
                display_name = COALESCE(
                    EXCLUDED.display_name, identity.person_identities.display_name
                ),
                updated_at = EXCLUDED.updated_at
            """,
            (ORG_SLUG,),
        )
        conn.execute(
            """
            INSERT INTO identity.person_memories (
                person_id, kind, key, value, source, created_at, updated_at
            )
            SELECT p.id, f.kind, f.fact_key, f.value, f.source,
                   f.created_at::timestamptz, f.updated_at::timestamptz
            FROM public.person_facts f
            JOIN identity.people p ON p.slug = f.member_key
            JOIN identity.organizations o ON o.id = p.organization_id AND o.slug = ?
            ON CONFLICT (person_id, kind, key) DO NOTHING
            """,
            (ORG_SLUG,),
        )
        conn.execute(
            """
            INSERT INTO identity.person_events (
                person_id, event_type, payload, telegram_message_id, created_at
            )
            SELECT p.id, e.event_type,
                   CASE
                     WHEN e.payload IS NULL OR btrim(e.payload) = '' THEN NULL
                     WHEN left(btrim(e.payload), 1) IN ('{', '[') THEN e.payload::jsonb
                     ELSE jsonb_build_object('raw', e.payload)
                   END,
                   e.telegram_message_id,
                   e.created_at::timestamptz
            FROM public.person_events e
            JOIN identity.people p ON p.slug = e.member_key
            """
        )
        conn.execute(
            """
            INSERT INTO work.tasks (
                id, organization_id, group_id, project_id, telegram_chat_id,
                title, description, status, due_date, created_by_person_id,
                created_at, updated_at, completed_at
            ) OVERRIDING SYSTEM VALUE
            SELECT
                t.id,
                o.id,
                g.id,
                CASE
                  WHEN t.title ILIKE '%شی%' OR COALESCE(t.description, '') ILIKE '%شی%'
                    OR t.title ILIKE '%SHEY%' OR COALESCE(t.description, '') ILIKE '%SHEY%'
                  THEN proj.id
                  ELSE NULL
                END,
                t.group_id,
                t.title,
                t.description,
                t.status,
                NULLIF(t.due_date, '')::date,
                creator.person_id,
                t.created_at::timestamptz,
                t.updated_at::timestamptz,
                NULLIF(t.completed_at, '')::timestamptz
            FROM public.tasks t
            JOIN identity.organizations o ON o.slug = ?
            LEFT JOIN identity.groups g ON g.telegram_chat_id = t.group_id
            LEFT JOIN work.projects proj
              ON proj.organization_id = o.id AND proj.slug = ?
            LEFT JOIN identity.person_identities creator
              ON creator.provider = 'telegram'
             AND creator.provider_user_id = t.created_by_user_id::text
            ON CONFLICT (id) DO NOTHING
            """,
            (ORG_SLUG, SHEY_SLUG),
        )
        conn.execute(
            """
            SELECT setval(
                pg_get_serial_sequence('work.tasks', 'id'),
                COALESCE((SELECT MAX(id) FROM work.tasks), 1)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO work.task_assignees (task_id, person_id, assigned_at)
            SELECT t.id, p.id, COALESCE(ot.updated_at::timestamptz, now())
            FROM public.tasks ot
            JOIN work.tasks t ON t.id = ot.id
            JOIN identity.people p ON p.slug = ot.assignee_key
            WHERE ot.assignee_key IS NOT NULL AND btrim(ot.assignee_key) <> ''
            ON CONFLICT (task_id, person_id) DO NOTHING
            """
        )
        conn.execute(
            """
            INSERT INTO comms.messages (
                group_id, person_id, telegram_chat_id, telegram_message_id,
                telegram_update_id, direction, kind, body, processed, created_at
            )
            SELECT
                g.id,
                pi.person_id,
                i.chat_id,
                i.message_id,
                i.telegram_update_id,
                'in',
                i.kind,
                i.text,
                (i.processed <> 0),
                i.created_at::timestamptz
            FROM public.inbox i
            LEFT JOIN identity.groups g ON g.telegram_chat_id = i.chat_id
            LEFT JOIN identity.person_identities pi
              ON pi.provider = 'telegram' AND pi.provider_user_id = i.user_id::text
            ON CONFLICT (telegram_update_id) DO NOTHING
            """
        )
        conn.execute(
            """
            INSERT INTO comms.message_media (message_id, file_id, storage_path)
            SELECT m.id, i.file_id, i.media_path
            FROM public.inbox i
            JOIN comms.messages m ON m.telegram_update_id = i.telegram_update_id
            WHERE i.file_id IS NOT NULL OR i.media_path IS NOT NULL
            """
        )
        conn.execute(
            """
            INSERT INTO comms.messages (
                group_id, person_id, telegram_chat_id, telegram_message_id,
                telegram_update_id, direction, kind, body, processed, created_at
            )
            SELECT
                g.id,
                pi.person_id,
                c.chat_id,
                c.telegram_message_id,
                NULL,
                'out',
                c.kind,
                c.text,
                true,
                c.created_at::timestamptz
            FROM public.conversation c
            LEFT JOIN identity.groups g ON g.telegram_chat_id = c.chat_id
            LEFT JOIN identity.person_identities pi
              ON pi.provider = 'telegram' AND pi.provider_user_id = c.user_id::text
            WHERE c.direction = 'out'
              AND NOT EXISTS (
                SELECT 1 FROM comms.messages m
                WHERE m.telegram_chat_id = c.chat_id
                  AND m.direction = 'out'
                  AND m.telegram_message_id IS NOT DISTINCT FROM c.telegram_message_id
              )
            """
        )
        conn.execute(
            """
            INSERT INTO comms.messages (
                group_id, person_id, telegram_chat_id, telegram_message_id,
                telegram_update_id, direction, kind, body, processed, created_at
            )
            SELECT
                g.id,
                COALESCE(pi.person_id, p.id),
                c.chat_id,
                c.telegram_message_id,
                NULL,
                'in',
                c.kind,
                c.text,
                true,
                c.created_at::timestamptz
            FROM public.conversation c
            LEFT JOIN identity.groups g ON g.telegram_chat_id = c.chat_id
            LEFT JOIN identity.person_identities pi
              ON pi.provider = 'telegram' AND pi.provider_user_id = c.user_id::text
            LEFT JOIN identity.people p ON p.slug = c.member_key
            WHERE c.direction = 'in'
              AND NOT EXISTS (
                SELECT 1 FROM comms.messages m
                WHERE m.telegram_chat_id = c.chat_id
                  AND m.direction = 'in'
                  AND m.telegram_message_id IS NOT DISTINCT FROM c.telegram_message_id
              )
            """
        )
        conn.execute(
            """
            INSERT INTO comms.message_media (message_id, storage_path)
            SELECT m.id, c.media_path
            FROM public.conversation c
            JOIN comms.messages m
              ON m.telegram_chat_id = c.chat_id
             AND m.telegram_message_id IS NOT DISTINCT FROM c.telegram_message_id
             AND m.direction = c.direction
            WHERE c.media_path IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM comms.message_media mm WHERE mm.message_id = m.id
              )
            """
        )
        conn.execute(
            """
            INSERT INTO comms.lessons (organization_id, body, source_person_id, created_at)
            SELECT o.id, l.lesson, p.id, l.created_at::timestamptz
            FROM public.lessons l
            JOIN identity.organizations o ON o.slug = ?
            LEFT JOIN identity.people p
              ON p.slug = CASE WHEN l.source = 'kaveh' THEN 'kawe' ELSE l.source END
             AND p.organization_id = o.id
            ON CONFLICT (organization_id, body) DO NOTHING
            """,
            (ORG_SLUG,),
        )
        conn.execute(
            """
            INSERT INTO ops.settings (organization_id, group_id, key, value, updated_at)
            SELECT o.id, g.id, kv.key, to_jsonb(kv.value), kv.updated_at::timestamptz
            FROM public.kv kv
            JOIN identity.organizations o ON o.slug = ?
            LEFT JOIN identity.groups g ON g.telegram_chat_id = ?
            WHERE kv.key NOT LIKE 'role_%'
              AND NOT EXISTS (
                SELECT 1 FROM ops.settings s
                WHERE s.organization_id = o.id
                  AND s.key = kv.key
                  AND s.group_id IS NOT DISTINCT FROM g.id
              )
            """,
            (ORG_SLUG, KNOWN_GROUP_CHAT_ID),
        )

    def _verify_postgres_migration(self, conn: _AdaptConn) -> None:
        people_n = conn.execute("SELECT count(*) AS n FROM identity.people").fetchone()["n"]
        tasks_n = conn.execute("SELECT count(*) AS n FROM work.tasks").fetchone()["n"]
        task_ids = [
            int(r["id"])
            for r in conn.execute("SELECT id FROM work.tasks ORDER BY id")
        ]
        hamed = conn.execute(
            """
            SELECT m.value
            FROM identity.person_memories m
            JOIN identity.people p ON p.id = m.person_id
            WHERE p.slug = 'hamed' AND m.kind = 'role' AND m.key = 'summary'
            """
        ).fetchone()
        missing = [
            r["slug"]
            for r in conn.execute(
                """
                SELECT p.slug
                FROM identity.people p
                WHERE p.kind = 'board'
                  AND p.status = 'active'
                  AND NOT EXISTS (
                    SELECT 1 FROM identity.person_memories m
                    WHERE m.person_id = p.id AND m.kind = 'role' AND m.key = 'summary'
                      AND TRIM(m.value) <> ''
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM identity.person_roles r WHERE r.person_id = p.id
                  )
                ORDER BY p.slug
                """
            )
        ]
        if int(people_n) < 5:
            raise RuntimeError(f"migration aborted: people count {people_n}")
        if int(tasks_n) != 10 or 3 not in task_ids:
            raise RuntimeError(f"migration aborted: task ids {task_ids}")
        if not hamed or not str(hamed["value"]).strip():
            raise RuntimeError("migration aborted: hamed role missing")
        if "hamed" in missing or "kawe" in missing:
            raise RuntimeError(f"migration aborted: missing roles {missing}")

    def _drop_postgres_legacy(self, conn: _AdaptConn) -> None:
        for table in (
            "person_facts",
            "person_events",
            "people",
            "tasks",
            "user_mappings",
            "inbox",
            "conversation",
            "kv",
            "lessons",
            "schema_migrations",
        ):
            conn.execute(f"DROP TABLE IF EXISTS public.{table} CASCADE")

    def _seed_all(self, conn: _AdaptConn) -> None:
        self._ensure_org(conn)
        if self._dsn:
            self._ensure_group(conn, KNOWN_GROUP_CHAT_ID, KNOWN_GROUP_TITLE)
        self._ensure_project(conn, SHEY_SLUG, SHEY_NAME)
        for member in BOARD_MEMBERS:
            self._ensure_person(
                conn,
                member.key,
                display_name=member.display_name,
                kind=self._person_kind(member.key),
            )
        self._seed_known_facts(conn)
        self._seed_known_roles(conn)

    def _person_kind(self, member_key: str, kind: str | None = None) -> str:
        if kind:
            return kind
        return "staff" if member_key in STAFF_KEYS else "board"

    def _person_display(self, member_key: str, display_name: str | None = None) -> str:
        if display_name:
            return display_name
        member = MEMBER_BY_KEY.get(member_key)
        return member.display_name if member else member_key

    def _ensure_org(self, conn: _AdaptConn) -> str:
        if self._org_id:
            row = conn.execute(
                "SELECT id FROM organizations WHERE id = ?", (self._org_id,)
            ).fetchone()
            if row:
                return str(row["id"])
        row = conn.execute(
            "SELECT id FROM organizations WHERE slug = ?", (ORG_SLUG,)
        ).fetchone()
        if row:
            self._org_id = str(row["id"])
            return self._org_id
        org_id = _new_id()
        conn.execute(
            """
            INSERT INTO organizations (id, slug, name, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (slug) DO NOTHING
            """,
            (org_id, ORG_SLUG, ORG_NAME, _dt_to_str(_utcnow())),
        )
        row = conn.execute(
            "SELECT id FROM organizations WHERE slug = ?", (ORG_SLUG,)
        ).fetchone()
        assert row is not None
        self._org_id = str(row["id"])
        return self._org_id

    def _ensure_group(
        self, conn: _AdaptConn, telegram_chat_id: int, title: str | None = None
    ) -> str:
        row = conn.execute(
            "SELECT id FROM groups WHERE telegram_chat_id = ?", (telegram_chat_id,)
        ).fetchone()
        if row:
            if title:
                conn.execute(
                    "UPDATE groups SET title = COALESCE(title, ?) WHERE id = ?",
                    (title, row["id"]),
                )
            return str(row["id"])
        group_id = _new_id()
        org_id = self._ensure_org(conn)
        conn.execute(
            """
            INSERT INTO groups (id, organization_id, telegram_chat_id, title, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (telegram_chat_id) DO NOTHING
            """,
            (group_id, org_id, telegram_chat_id, title, _dt_to_str(_utcnow())),
        )
        row = conn.execute(
            "SELECT id FROM groups WHERE telegram_chat_id = ?", (telegram_chat_id,)
        ).fetchone()
        assert row is not None
        return str(row["id"])

    def _ensure_project(self, conn: _AdaptConn, slug: str, name: str) -> str:
        org_id = self._ensure_org(conn)
        row = conn.execute(
            "SELECT id FROM projects WHERE organization_id = ? AND slug = ?",
            (org_id, slug),
        ).fetchone()
        if row:
            return str(row["id"])
        project_id = _new_id()
        conn.execute(
            """
            INSERT INTO projects (id, organization_id, slug, name, status, created_at)
            VALUES (?, ?, ?, ?, 'active', ?)
            ON CONFLICT (organization_id, slug) DO NOTHING
            """,
            (project_id, org_id, slug, name, _dt_to_str(_utcnow())),
        )
        row = conn.execute(
            "SELECT id FROM projects WHERE organization_id = ? AND slug = ?",
            (org_id, slug),
        ).fetchone()
        assert row is not None
        return str(row["id"])

    def _ensure_person(
        self,
        conn: Any,
        member_key: str,
        display_name: str | None = None,
        kind: str | None = None,
    ) -> str:
        org_id = self._ensure_org(conn)
        row = conn.execute(
            "SELECT id FROM people WHERE organization_id = ? AND slug = ?",
            (org_id, member_key),
        ).fetchone()
        if row:
            return str(row["id"])
        person_id = _new_id()
        now = _dt_to_str(_utcnow())
        conn.execute(
            """
            INSERT INTO people (
                id, organization_id, slug, display_name, kind, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
            ON CONFLICT (organization_id, slug) DO NOTHING
            """,
            (
                person_id,
                org_id,
                member_key,
                self._person_display(member_key, display_name),
                self._person_kind(member_key, kind),
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT id FROM people WHERE organization_id = ? AND slug = ?",
            (org_id, member_key),
        ).fetchone()
        assert row is not None
        return str(row["id"])

    def _person_id(self, conn: Any, member_key: str) -> str | None:
        org_id = self._ensure_org(conn)
        row = conn.execute(
            "SELECT id FROM people WHERE organization_id = ? AND slug = ?",
            (org_id, member_key),
        ).fetchone()
        return None if not row else str(row["id"])

    def _person_id_by_telegram(self, conn: Any, telegram_user_id: int | None) -> str | None:
        if telegram_user_id is None:
            return None
        row = conn.execute(
            """
            SELECT person_id FROM person_identities
            WHERE provider = 'telegram' AND provider_user_id = ?
            """,
            (str(telegram_user_id),),
        ).fetchone()
        return None if not row else str(row["person_id"])

    def _seed_known_facts(self, conn: Any) -> None:
        now = _dt_to_str(_utcnow())
        for member_key, kind, fact_key, value, source in SEEDED_FACTS:
            person_id = self._ensure_person(conn, member_key)
            conn.execute(
                """
                INSERT INTO person_memories (
                    id, person_id, kind, key, value, source, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (person_id, kind, key) DO NOTHING
                """,
                (_new_id(), person_id, kind, fact_key, value, source, now, now),
            )

    def _seed_known_roles(self, conn: Any) -> None:
        for member_key, kind, fact_key, value, source in SEEDED_FACTS:
            if kind != "role" or fact_key != "summary":
                continue
            person_id = self._ensure_person(conn, member_key)
            self._ensure_roles_from_summary(conn, person_id, value, source)

    def _ensure_roles_from_summary(
        self, conn: Any, person_id: str, value: str, source: str | None
    ) -> None:
        titles = parse_role_titles(value)
        rows = conn.execute(
            "SELECT title, source FROM person_roles WHERE person_id = ? ORDER BY created_at",
            (person_id,),
        ).fetchall()
        existing = [r["title"] for r in rows]
        sources = {r["source"] for r in rows}
        if not rows:
            self._insert_roles(conn, person_id, titles, source)
            return
        if existing == titles:
            return
        if sources <= {None, "seed"}:
            self._replace_roles(conn, person_id, titles, source)

    def _insert_roles(
        self, conn: Any, person_id: str, titles: list[str], source: str | None
    ) -> None:
        now = _dt_to_str(_utcnow())
        for i, title in enumerate(titles):
            conn.execute(
                """
                INSERT INTO person_roles (
                    id, person_id, title, department, is_primary, source, created_at
                ) VALUES (?, ?, ?, NULL, ?, ?, ?)
                """,
                (_new_id(), person_id, title, i == 0, source, now),
            )

    def _replace_roles(
        self, conn: Any, person_id: str, titles: list[str], source: str | None
    ) -> None:
        conn.execute("DELETE FROM person_roles WHERE person_id = ?", (person_id,))
        self._insert_roles(conn, person_id, titles, source)

    def _upsert_telegram_identity(
        self,
        conn: Any,
        person_id: str,
        telegram_user_id: int,
        username: str | None,
        display_name: str | None,
    ) -> None:
        now = _dt_to_str(_utcnow())
        uid = str(telegram_user_id)
        conn.execute(
            """
            DELETE FROM person_identities
            WHERE provider = 'telegram' AND provider_user_id = ? AND person_id != ?
            """,
            (uid, person_id),
        )
        existing = conn.execute(
            """
            SELECT id FROM person_identities
            WHERE person_id = ? AND provider = 'telegram'
            """,
            (person_id,),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE person_identities
                SET provider_user_id = ?,
                    username = COALESCE(?, username),
                    display_name = COALESCE(?, display_name),
                    is_primary = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (uid, username, display_name, True, now, existing["id"]),
            )
            return
        conn.execute(
            """
            INSERT INTO person_identities (
                id, person_id, provider, provider_user_id, username, display_name,
                is_primary, updated_at
            ) VALUES (?, ?, 'telegram', ?, ?, ?, ?, ?)
            """,
            (_new_id(), person_id, uid, username, display_name, True, now),
        )

    def _fetch_task(
        self, conn: Any, task_id: int, group_id: int | None = None
    ) -> Task | None:
        if group_id is not None:
            row = conn.execute(
                TASK_SELECT + " WHERE t.id = ? AND t.telegram_chat_id = ?",
                (task_id, group_id),
            ).fetchone()
        else:
            row = conn.execute(TASK_SELECT + " WHERE t.id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    def _dump_setting(self, value: str) -> Any:
        if self._dsn:
            return Jsonb(value)
        return json.dumps(value, ensure_ascii=False)

    def _load_setting(self, value: Any) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, str):
            try:
                loaded = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                return value
            if isinstance(loaded, str):
                return loaded
            if isinstance(loaded, (dict, list)):
                return json.dumps(loaded, ensure_ascii=False)
            return str(loaded)
        return str(value)

    def _default_group_id(self, conn: Any) -> str | None:
        row = conn.execute(
            """
            SELECT id FROM groups
            WHERE organization_id = ?
            ORDER BY CASE WHEN telegram_chat_id = ? THEN 0 ELSE 1 END, created_at
            LIMIT 1
            """,
            (self._ensure_org(conn), KNOWN_GROUP_CHAT_ID),
        ).fetchone()
        return None if not row else str(row["id"])

    def ensure_person(
        self,
        member_key: str,
        display_name: str | None = None,
        kind: str | None = None,
    ) -> None:
        with self._conn() as conn:
            person_id = self._ensure_person(
                conn, member_key, display_name=display_name, kind=kind
            )
            if display_name is None and kind is None:
                return
            now = _dt_to_str(_utcnow())
            fields = ["updated_at = ?"]
            values: list[object] = [now]
            if display_name is not None:
                fields.append("display_name = ?")
                values.append(display_name)
            if kind is not None:
                fields.append("kind = ?")
                values.append(kind)
            values.append(person_id)
            conn.execute(
                f"UPDATE people SET {', '.join(fields)} WHERE id = ?",
                values,
            )

    def upsert_person_identity(
        self,
        member_key: str,
        telegram_user_id: int,
        username: str | None,
        display_name: str | None,
    ) -> None:
        with self._conn() as conn:
            person_id = self._ensure_person(conn, member_key, display_name=display_name)
            if display_name:
                conn.execute(
                    "UPDATE people SET display_name = ?, updated_at = ? WHERE id = ?",
                    (display_name, _dt_to_str(_utcnow()), person_id),
                )
            self._upsert_telegram_identity(
                conn, person_id, telegram_user_id, username, display_name
            )

    def set_person_fact(
        self,
        member_key: str,
        kind: str,
        fact_key: str,
        value: str,
        source: str | None = None,
    ) -> None:
        now = _dt_to_str(_utcnow())
        with self._conn() as conn:
            person_id = self._ensure_person(conn, member_key)
            conn.execute(
                """
                INSERT INTO person_memories (
                    id, person_id, kind, key, value, source, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (person_id, kind, key) DO UPDATE SET
                    value = excluded.value,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (_new_id(), person_id, kind, fact_key, value, source, now, now),
            )
            if kind == "role" and fact_key == "summary":
                self._replace_roles(conn, person_id, parse_role_titles(value), source)

    def get_person_fact(self, member_key: str, kind: str, fact_key: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT m.value
                FROM person_memories m
                JOIN people p ON p.id = m.person_id
                WHERE p.slug = ? AND m.kind = ? AND m.key = ?
                """,
                (member_key, kind, fact_key),
            ).fetchone()
        return None if not row else row["value"]

    def list_person_facts(self, member_key: str, kind: str | None = None) -> list[dict]:
        with self._conn() as conn:
            if kind is None:
                rows = conn.execute(
                    """
                    SELECT m.id, p.slug AS member_key, m.kind, m.key, m.key AS fact_key,
                           m.value, m.source, m.created_at, m.updated_at
                    FROM person_memories m
                    JOIN people p ON p.id = m.person_id
                    WHERE p.slug = ?
                    ORDER BY m.kind, m.key, m.id
                    """,
                    (member_key,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT m.id, p.slug AS member_key, m.kind, m.key, m.key AS fact_key,
                           m.value, m.source, m.created_at, m.updated_at
                    FROM person_memories m
                    JOIN people p ON p.id = m.person_id
                    WHERE p.slug = ? AND m.kind = ?
                    ORDER BY m.key, m.id
                    """,
                    (member_key, kind),
                ).fetchall()
        return [dict(r) for r in rows]

    def get_person_role(self, member_key: str) -> str | None:
        value = self.get_person_fact(member_key, "role", "summary")
        if value and str(value).strip():
            return value
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT r.title
                FROM person_roles r
                JOIN people p ON p.id = r.person_id
                WHERE p.slug = ?
                ORDER BY r.is_primary DESC, r.created_at ASC
                """,
                (member_key,),
            ).fetchall()
        titles = [r["title"] for r in rows if r["title"]]
        if not titles:
            return None
        return "\u060c ".join(titles)

    def set_person_role(
        self, member_key: str, value: str, source: str | None = None
    ) -> None:
        self.set_person_fact(member_key, "role", "summary", value, source=source)

    def list_people_missing_role(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT p.slug AS member_key
                FROM people p
                WHERE p.kind = 'board'
                  AND p.status = 'active'
                  AND NOT EXISTS (
                    SELECT 1 FROM person_memories m
                    WHERE m.person_id = p.id
                      AND m.kind = 'role'
                      AND m.key = 'summary'
                      AND TRIM(m.value) <> ''
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM person_roles r WHERE r.person_id = p.id
                  )
                ORDER BY p.slug
                """
            ).fetchall()
        return [r["member_key"] for r in rows]

    def log_person_event(
        self,
        member_key: str,
        event_type: str,
        payload: str | dict | None = None,
        telegram_message_id: int | None = None,
    ) -> None:
        if payload is None:
            stored: Any = None
        elif isinstance(payload, str):
            if self._dsn:
                try:
                    stored = Jsonb(json.loads(payload))
                except (TypeError, ValueError, json.JSONDecodeError):
                    stored = Jsonb({"raw": payload})
            else:
                stored = payload
        else:
            stored = Jsonb(payload) if self._dsn else json.dumps(payload, ensure_ascii=False)
        now = _dt_to_str(_utcnow())
        with self._conn() as conn:
            person_id = self._ensure_person(conn, member_key)
            conn.execute(
                """
                INSERT INTO person_events (
                    person_id, event_type, payload, telegram_message_id, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (person_id, event_type, stored, telegram_message_id, now),
            )

    def list_person_events(self, member_key: str, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT e.id, p.slug AS member_key, e.event_type, e.payload,
                       e.telegram_message_id, e.created_at
                FROM person_events e
                JOIN people p ON p.id = e.person_id
                WHERE p.slug = ?
                ORDER BY e.id DESC
                LIMIT ?
                """,
                (member_key, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def add_lesson(self, lesson: str, source: str | None = None) -> None:
        text = lesson.strip()
        if not text:
            return
        now = _dt_to_str(_utcnow())
        with self._conn() as conn:
            org_id = self._ensure_org(conn)
            source_person_id = None
            if source:
                slug = "kawe" if source == "kaveh" else source
                source_person_id = self._person_id(conn, slug)
            conn.execute(
                """
                INSERT INTO lessons (id, organization_id, body, source_person_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (organization_id, body) DO NOTHING
                """,
                (_new_id(), org_id, text, source_person_id, now),
            )

    def list_lessons(self) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT body FROM lessons ORDER BY created_at, id"
            ).fetchall()
        return [r["body"] for r in rows]

    def _log_task_event(
        self,
        conn: Any,
        task_id: int,
        event_type: str,
        payload: dict | None = None,
        actor_person_id: str | None = None,
    ) -> None:
        if payload is None:
            stored: Any = None
        else:
            stored = Jsonb(payload) if self._dsn else json.dumps(payload, ensure_ascii=False)
        conn.execute(
            """
            INSERT INTO task_events (task_id, event_type, payload, actor_person_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task_id, event_type, stored, actor_person_id, _dt_to_str(_utcnow())),
        )

    def _actor_person_id(
        self,
        conn: Any,
        *,
        actor_key: str | None = None,
        actor_user_id: int | None = None,
    ) -> str | None:
        if actor_key:
            return self._ensure_person(conn, actor_key)
        return self._person_id_by_telegram(conn, actor_user_id)

    def create_task(
        self,
        *,
        group_id: int,
        title: str,
        description: str | None = None,
        assignee_key: str | None = None,
        due_date: date | None = None,
        created_by_user_id: int | None = None,
    ) -> Task:
        from charbot.intent import is_question_shaped_title

        cleaned = (title or "").strip()
        if not cleaned or is_question_shaped_title(cleaned):
            raise ValueError("refused question-shaped task title")
        now = _utcnow()
        now_s = _dt_to_str(now)
        with self._conn() as conn:
            org_id = self._ensure_org(conn)
            group_uuid = self._ensure_group(conn, group_id)
            project_id = None
            if _looks_like_shey(title, description):
                project_id = self._ensure_project(conn, SHEY_SLUG, SHEY_NAME)
            created_by_person_id = self._person_id_by_telegram(conn, created_by_user_id)
            due_s = due_date.isoformat() if due_date else None
            cur = conn.execute(
                """
                INSERT INTO tasks (
                    organization_id, group_id, project_id, telegram_chat_id,
                    title, description, status, due_date, created_by_person_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    org_id,
                    group_uuid,
                    project_id,
                    group_id,
                    title.strip(),
                    description,
                    TaskStatus.OPEN.value,
                    due_s,
                    created_by_person_id,
                    now_s,
                    now_s,
                ),
            )
            inserted = cur.fetchone()
            assert inserted is not None
            task_id = int(inserted["id"])
            if assignee_key:
                person_id = self._ensure_person(conn, assignee_key)
                conn.execute(
                    """
                    INSERT INTO task_assignees (task_id, person_id, assigned_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT (task_id, person_id) DO NOTHING
                    """,
                    (task_id, person_id, now_s),
                )
                self._log_task_event(
                    conn,
                    task_id,
                    "assigned",
                    {"assignee_key": assignee_key},
                    created_by_person_id,
                )
            task = self._fetch_task(conn, task_id, group_id)
        assert task is not None
        return task

    def get_task(self, task_id: int, group_id: int | None = None) -> Task | None:
        with self._conn() as conn:
            return self._fetch_task(conn, task_id, group_id)

    def list_open_tasks(self, group_id: int) -> list[Task]:
        with self._conn() as conn:
            rows = conn.execute(
                TASK_SELECT
                + """
                WHERE t.telegram_chat_id = ? AND t.status IN ('open', 'in_progress')
                ORDER BY t.due_date IS NULL, t.due_date ASC, t.id ASC
                """,
                (group_id,),
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    def list_overdue_tasks(self, group_id: int, today: date | None = None) -> list[Task]:
        today = today or date.today()
        with self._conn() as conn:
            rows = conn.execute(
                TASK_SELECT
                + """
                WHERE t.telegram_chat_id = ?
                  AND t.status IN ('open', 'in_progress')
                  AND t.due_date IS NOT NULL
                  AND t.due_date < ?
                ORDER BY t.due_date ASC, t.id ASC
                """,
                (group_id, today.isoformat()),
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    def list_unowned_open_tasks(self, group_id: int) -> list[Task]:
        with self._conn() as conn:
            rows = conn.execute(
                TASK_SELECT
                + """
                WHERE t.telegram_chat_id = ?
                  AND t.status IN ('open', 'in_progress')
                  AND NOT EXISTS (
                    SELECT 1 FROM task_assignees a WHERE a.task_id = t.id
                  )
                ORDER BY t.id ASC
                """,
                (group_id,),
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    def assign_task(
        self,
        task_id: int,
        group_id: int,
        assignee_key: str,
        *,
        actor_key: str | None = None,
        actor_user_id: int | None = None,
    ) -> Task | None:
        now = _dt_to_str(_utcnow())
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM tasks WHERE id = ? AND telegram_chat_id = ?",
                (task_id, group_id),
            ).fetchone()
            if not row:
                return None
            person_id = self._ensure_person(conn, assignee_key)
            conn.execute("DELETE FROM task_assignees WHERE task_id = ?", (task_id,))
            conn.execute(
                """
                INSERT INTO task_assignees (task_id, person_id, assigned_at)
                VALUES (?, ?, ?)
                """,
                (task_id, person_id, now),
            )
            conn.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?",
                (now, task_id),
            )
            actor = self._actor_person_id(conn, actor_key=actor_key, actor_user_id=actor_user_id)
            self._log_task_event(
                conn, task_id, "assigned", {"assignee_key": assignee_key}, actor
            )
            return self._fetch_task(conn, task_id, group_id)

    def update_task_fields(
        self,
        task_id: int,
        group_id: int,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> Task | None:
        now = _dt_to_str(_utcnow())
        with self._conn() as conn:
            current = self._fetch_task(conn, task_id, group_id)
            if current is None:
                return None
            new_title = title if title is not None else current.title
            new_desc = description if description is not None else current.description
            conn.execute(
                """
                UPDATE tasks SET title = ?, description = ?, updated_at = ?
                WHERE id = ? AND telegram_chat_id = ?
                """,
                (new_title, new_desc, now, task_id, group_id),
            )
            return self._fetch_task(conn, task_id, group_id)

    def set_due_date(self, task_id: int, group_id: int, due_date: date) -> Task | None:

        now = _dt_to_str(_utcnow())
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE tasks SET due_date = ?, updated_at = ?
                WHERE id = ? AND telegram_chat_id = ?
                """,
                (due_date.isoformat(), now, task_id, group_id),
            )
            return self._fetch_task(conn, task_id, group_id)

    def mark_done(
        self,
        task_id: int,
        group_id: int,
        *,
        actor_key: str | None = None,
        actor_user_id: int | None = None,
    ) -> Task | None:
        return self.set_status(
            task_id,
            group_id,
            TaskStatus.DONE,
            actor_key=actor_key,
            actor_user_id=actor_user_id,
        )

    def set_status(
        self,
        task_id: int,
        group_id: int,
        status: TaskStatus,
        *,
        actor_key: str | None = None,
        actor_user_id: int | None = None,
    ) -> Task | None:
        now = _utcnow()
        now_s = _dt_to_str(now)
        with self._conn() as conn:
            current = self._fetch_task(conn, task_id, group_id)
            if current is None:
                return None
            if status == TaskStatus.DONE:
                completed = (
                    _dt_to_str(current.completed_at) if current.completed_at else now_s
                )
            else:
                completed = None
            conn.execute(
                """
                UPDATE tasks SET status = ?, completed_at = ?, updated_at = ?
                WHERE id = ? AND telegram_chat_id = ?
                """,
                (status.value, completed, now_s, task_id, group_id),
            )
            actor = self._actor_person_id(
                conn, actor_key=actor_key, actor_user_id=actor_user_id
            )
            if status == TaskStatus.DONE and current.status != TaskStatus.DONE:
                self._log_task_event(conn, task_id, "done", {"status": "done"}, actor)
            elif (
                status != TaskStatus.DONE
                and current.status == TaskStatus.DONE
            ):
                self._log_task_event(
                    conn, task_id, "reopened", {"status": status.value}, actor
                )
            return self._fetch_task(conn, task_id, group_id)

    def list_group_tasks(self, group_id: int) -> list[Task]:
        with self._conn() as conn:
            rows = conn.execute(
                TASK_SELECT + " WHERE t.telegram_chat_id = ? ORDER BY t.id ASC",
                (group_id,),
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    def list_task_events(self, task_id: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, task_id, event_type, payload, actor_person_id, created_at
                FROM task_events
                WHERE task_id = ?
                ORDER BY id ASC
                """,
                (task_id,),
            ).fetchall()
        out = []
        for r in rows:
            payload = r["payload"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (TypeError, ValueError, json.JSONDecodeError):
                    payload = {"raw": payload}
            out.append(
                {
                    "id": r["id"],
                    "task_id": r["task_id"],
                    "event_type": r["event_type"],
                    "payload": payload,
                    "actor_person_id": r["actor_person_id"],
                    "created_at": r["created_at"],
                }
            )
        return out

    def upsert_user_mapping(
        self,
        *,
        telegram_user_id: int,
        member_key: str,
        username: str | None = None,
        display_name: str | None = None,
    ) -> TelegramUserMapping:
        with self._conn() as conn:
            person_id = self._ensure_person(conn, member_key, display_name=display_name)
            if display_name:
                conn.execute(
                    "UPDATE people SET display_name = ?, updated_at = ? WHERE id = ?",
                    (display_name, _dt_to_str(_utcnow()), person_id),
                )
            self._upsert_telegram_identity(
                conn, person_id, telegram_user_id, username, display_name
            )
        mapping = self.get_user_mapping(telegram_user_id)
        assert mapping is not None
        return mapping

    def get_user_mapping(self, telegram_user_id: int) -> TelegramUserMapping | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT pi.provider_user_id AS telegram_user_id,
                       p.slug AS member_key,
                       pi.username,
                       COALESCE(pi.display_name, p.display_name) AS display_name
                FROM person_identities pi
                JOIN people p ON p.id = pi.person_id
                WHERE pi.provider = 'telegram' AND pi.provider_user_id = ?
                """,
                (str(telegram_user_id),),
            ).fetchone()
        if not row:
            return None
        return TelegramUserMapping(
            telegram_user_id=int(row["telegram_user_id"]),
            member_key=row["member_key"],
            username=row["username"],
            display_name=row["display_name"],
        )

    def list_user_mappings(self) -> list[TelegramUserMapping]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT pi.provider_user_id AS telegram_user_id,
                       p.slug AS member_key,
                       pi.username,
                       COALESCE(pi.display_name, p.display_name) AS display_name
                FROM person_identities pi
                JOIN people p ON p.id = pi.person_id
                WHERE pi.provider = 'telegram'
                ORDER BY p.slug
                """
            ).fetchall()
        return [
            TelegramUserMapping(
                telegram_user_id=int(r["telegram_user_id"]),
                member_key=r["member_key"],
                username=r["username"],
                display_name=r["display_name"],
            )
            for r in rows
        ]

    def set_kv(self, key: str, value: str) -> None:
        now = _dt_to_str(_utcnow())
        with self._conn() as conn:
            org_id = self._ensure_org(conn)
            group_uuid: str | None = None
            if key == "telegram_group_id":
                try:
                    group_uuid = self._ensure_group(conn, int(value))
                except (TypeError, ValueError):
                    group_uuid = self._default_group_id(conn)
            else:
                group_uuid = self._default_group_id(conn)
            stored = self._dump_setting(value)
            if group_uuid is None:
                existing = conn.execute(
                    """
                    SELECT 1 FROM settings
                    WHERE organization_id = ? AND key = ? AND group_id IS NULL
                    """,
                    (org_id, key),
                ).fetchone()
                if existing:
                    conn.execute(
                        """
                        UPDATE settings SET value = ?, updated_at = ?
                        WHERE organization_id = ? AND key = ? AND group_id IS NULL
                        """,
                        (stored, now, org_id, key),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO settings (organization_id, group_id, key, value, updated_at)
                        VALUES (?, NULL, ?, ?, ?)
                        """,
                        (org_id, key, stored, now),
                    )
            else:
                existing = conn.execute(
                    """
                    SELECT 1 FROM settings
                    WHERE organization_id = ? AND key = ? AND group_id = ?
                    """,
                    (org_id, key, group_uuid),
                ).fetchone()
                if existing:
                    conn.execute(
                        """
                        UPDATE settings SET value = ?, updated_at = ?
                        WHERE organization_id = ? AND key = ? AND group_id = ?
                        """,
                        (stored, now, org_id, key, group_uuid),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO settings (organization_id, group_id, key, value, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (org_id, group_uuid, key, stored, now),
                    )

    def get_kv(self, key: str) -> str | None:
        with self._conn() as conn:
            org_id = self._ensure_org(conn)
            row = conn.execute(
                """
                SELECT value FROM settings
                WHERE organization_id = ? AND key = ?
                ORDER BY CASE WHEN group_id IS NULL THEN 1 ELSE 0 END
                LIMIT 1
                """,
                (org_id, key),
            ).fetchone()
        return None if not row else self._load_setting(row["value"])

    def log_inbox(
        self,
        *,
        telegram_update_id: int | None,
        chat_id: int,
        chat_type: str | None,
        chat_title: str | None,
        user_id: int | None,
        username: str | None,
        display_name: str | None,
        message_id: int | None,
        kind: str,
        text: str | None,
        file_id: str | None = None,
        media_path: str | None = None,
    ) -> None:
        del chat_type
        now = _dt_to_str(_utcnow())
        with self._conn() as conn:
            group_uuid = self._ensure_group(conn, chat_id, title=chat_title)
            person_id = self._person_id_by_telegram(conn, user_id)
            if person_id and (username or display_name) and user_id is not None:
                self._upsert_telegram_identity(
                    conn, person_id, user_id, username, display_name
                )
            cur = conn.execute(
                """
                INSERT INTO messages (
                    group_id, person_id, telegram_chat_id, telegram_message_id,
                    telegram_update_id, direction, kind, body, processed, created_at
                ) VALUES (?, ?, ?, ?, ?, 'in', ?, ?, ?, ?)
                ON CONFLICT (telegram_update_id) DO NOTHING
                RETURNING id
                """,
                (
                    group_uuid,
                    person_id,
                    chat_id,
                    message_id,
                    telegram_update_id,
                    kind,
                    text,
                    False,
                    now,
                ),
            )
            inserted = cur.fetchone()
            message_row_id = None if not inserted else int(inserted["id"])
            if message_row_id is None and telegram_update_id is not None:
                found = conn.execute(
                    "SELECT id FROM messages WHERE telegram_update_id = ?",
                    (telegram_update_id,),
                ).fetchone()
                message_row_id = None if not found else int(found["id"])
            if message_row_id is not None and (file_id or media_path):
                conn.execute(
                    """
                    INSERT INTO message_media (message_id, file_id, storage_path, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (message_row_id, file_id, media_path, now),
                )

    def list_unprocessed_inbox(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    m.id,
                    m.telegram_update_id,
                    m.telegram_chat_id AS chat_id,
                    g.title AS chat_title,
                    pi.provider_user_id AS user_id,
                    pi.username,
                    COALESCE(pi.display_name, p.display_name) AS display_name,
                    m.telegram_message_id AS message_id,
                    m.kind,
                    m.body AS text,
                    (
                        SELECT mm.file_id FROM message_media mm
                        WHERE mm.message_id = m.id ORDER BY mm.id LIMIT 1
                    ) AS file_id,
                    (
                        SELECT mm.storage_path FROM message_media mm
                        WHERE mm.message_id = m.id ORDER BY mm.id LIMIT 1
                    ) AS media_path,
                    m.processed,
                    m.created_at
                FROM messages m
                LEFT JOIN groups g ON g.id = m.group_id
                LEFT JOIN people p ON p.id = m.person_id
                LEFT JOIN person_identities pi
                  ON pi.person_id = p.id AND pi.provider = 'telegram' AND pi.is_primary = ?
                WHERE m.direction = 'in' AND m.processed = ?
                ORDER BY m.id ASC
                LIMIT ?
                """,
                (True, False, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_inbox_processed(self, inbox_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE messages SET processed = ? WHERE id = ?",
                (True, inbox_id),
            )

    def log_conversation(
        self,
        *,
        chat_id: int,
        direction: str,
        kind: str,
        text: str | None,
        user_id: int | None = None,
        username: str | None = None,
        display_name: str | None = None,
        media_path: str | None = None,
        telegram_message_id: int | None = None,
        member_key: str | None = None,
    ) -> None:
        now = _dt_to_str(_utcnow())
        processed = direction == "out"
        with self._conn() as conn:
            group_uuid = self._ensure_group(conn, chat_id)
            person_id = self._person_id_by_telegram(conn, user_id)
            if person_id is None and member_key:
                person_id = self._person_id(conn, member_key)
            existing = None
            if telegram_message_id is not None:
                existing = conn.execute(
                    """
                    SELECT id FROM messages
                    WHERE telegram_chat_id = ?
                      AND telegram_message_id = ?
                      AND direction = ?
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    (chat_id, telegram_message_id, direction),
                ).fetchone()
            if existing:
                if person_id:
                    conn.execute(
                        "UPDATE messages SET person_id = COALESCE(person_id, ?) WHERE id = ?",
                        (person_id, existing["id"]),
                    )
                if media_path:
                    conn.execute(
                        """
                        INSERT INTO message_media (message_id, storage_path, created_at)
                        SELECT ?, ?, ?
                        WHERE NOT EXISTS (
                            SELECT 1 FROM message_media WHERE message_id = ?
                        )
                        """,
                        (existing["id"], media_path, now, existing["id"]),
                    )
                return
            cur = conn.execute(
                """
                INSERT INTO messages (
                    group_id, person_id, telegram_chat_id, telegram_message_id,
                    direction, kind, body, processed, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    group_uuid,
                    person_id,
                    chat_id,
                    telegram_message_id,
                    direction,
                    kind,
                    text,
                    processed,
                    now,
                ),
            )
            inserted = cur.fetchone()
            if inserted and media_path:
                conn.execute(
                    """
                    INSERT INTO message_media (message_id, storage_path, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (inserted["id"], media_path, now),
                )

    def update_message_body(
        self,
        body: str,
        *,
        message_id: int | None = None,
        telegram_chat_id: int | None = None,
        telegram_message_id: int | None = None,
        processed: bool | None = True,
    ) -> int | None:
        """Set comms.messages.body (transcript) and optionally mark processed."""
        if message_id is None and (telegram_chat_id is None or telegram_message_id is None):
            raise ValueError("message_id or telegram_chat_id+telegram_message_id required")
        with self._conn() as conn:
            if message_id is not None:
                row = conn.execute(
                    "SELECT id FROM messages WHERE id = ?",
                    (message_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT id FROM messages
                    WHERE telegram_chat_id = ? AND telegram_message_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (telegram_chat_id, telegram_message_id),
                ).fetchone()
            if not row:
                return None
            rid = int(row["id"])
            if processed is None:
                conn.execute("UPDATE messages SET body = ? WHERE id = ?", (body, rid))
            else:
                conn.execute(
                    "UPDATE messages SET body = ?, processed = ? WHERE id = ?",
                    (body, processed, rid),
                )
            return rid

    def get_latest_voice_message(
        self,
        *,
        chat_id: int | None = None,
        member_key: str | None = None,
        telegram_message_id: int | None = None,
        require_body: bool = False,
    ) -> dict | None:
        clauses = ["m.kind IN ('voice', 'audio')"]
        params: list[Any] = []
        if chat_id is not None:
            clauses.append("m.telegram_chat_id = ?")
            params.append(chat_id)
        if member_key:
            clauses.append("p.slug = ?")
            params.append(member_key)
        if telegram_message_id is not None:
            clauses.append("m.telegram_message_id = ?")
            params.append(telegram_message_id)
        if require_body:
            clauses.append("m.body IS NOT NULL AND TRIM(m.body) <> ''")
        where = " AND ".join(clauses)
        sql = (
            "SELECT m.id, m.body, m.kind, m.telegram_chat_id, m.telegram_message_id, "
            "m.processed, p.slug AS member_key, p.display_name, "
            "(SELECT mm.file_id FROM message_media mm WHERE mm.message_id = m.id "
            "ORDER BY mm.id LIMIT 1) AS file_id, "
            "(SELECT mm.storage_path FROM message_media mm WHERE mm.message_id = m.id "
            "ORDER BY mm.id LIMIT 1) AS media_path "
            "FROM messages m LEFT JOIN people p ON p.id = m.person_id "
            "WHERE " + where + " ORDER BY m.id DESC LIMIT 1"
        )
        with self._conn() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
        return None if not row else dict(row)

    def list_recent_human_messages(
        self,
        chat_id: int,
        *,
        exclude_telegram_message_id: int | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Latest inbound human texts in this chat (comms.messages), newest first."""
        clauses = [
            "m.telegram_chat_id = ?",
            "m.direction = 'in'",
            "m.body IS NOT NULL",
            "TRIM(m.body) <> ''",
        ]
        params: list[Any] = [chat_id]
        if exclude_telegram_message_id is not None:
            clauses.append("(m.telegram_message_id IS NULL OR m.telegram_message_id != ?)")
            params.append(exclude_telegram_message_id)
        params.append(limit)
        sql = (
            "SELECT m.id, m.body, m.telegram_message_id, p.slug AS member_key "
            "FROM messages m LEFT JOIN people p ON p.id = m.person_id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY m.id DESC LIMIT ?"
        )
        with self._conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

