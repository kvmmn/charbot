"""Tests for per-person identity and memory."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from charbot.store import TaskStore


def test_people_seeded(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "people.db")
    with store._conn() as conn:
        rows = conn.execute(
            "SELECT slug, kind, display_name FROM people ORDER BY slug"
        ).fetchall()
    by_key = {r["slug"]: r for r in rows}
    assert set(by_key) == {"ghazal", "hamed", "kawe", "mohammadreza", "saman"}
    assert by_key["ghazal"]["kind"] == "staff"
    assert by_key["kawe"]["kind"] == "board"
    assert by_key["hamed"]["kind"] == "board"
    assert by_key["saman"]["kind"] == "board"
    assert by_key["mohammadreza"]["kind"] == "board"


def test_roles_isolated_per_person(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "roles.db")
    store.set_person_role("hamed", "مدیرعامل تست", source="test")
    assert store.get_person_role("hamed") == "مدیرعامل تست"
    assert store.get_person_role("saman") is None
    store.set_person_role("saman", "هماهنگ‌کننده", source="test")
    assert store.get_person_role("hamed") == "مدیرعامل تست"
    assert store.get_person_role("saman") == "هماهنگ‌کننده"
    facts = store.list_person_facts("hamed", kind="role")
    assert {f["value"] for f in facts} == {"مدیرعامل تست"}


def test_missing_role_list(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "missing.db")
    missing = store.list_people_missing_role()
    assert "kawe" not in missing
    assert "hamed" not in missing
    assert "ghazal" not in missing
    assert missing == ["mohammadreza", "saman"]
    store.set_person_role("saman", "عضو هیئت")
    assert store.list_people_missing_role() == ["mohammadreza"]


def test_wal_pragma(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "wal.db")
    with store._conn() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert str(mode).lower() == "wal"
    assert int(fk) == 1
    assert int(busy) == 5000


def test_migrate_kv_roles_into_person_facts(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    raw = sqlite3.connect(db)
    raw.executescript(
        """
        CREATE TABLE kv (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO kv (key, value, updated_at) VALUES
            ('role_saman', 'طراح محصول از kv', '2026-01-01T00:00:00+00:00'),
            ('telegram_group_id', '-100', '2026-01-01T00:00:00+00:00');
        """
    )
    raw.commit()
    raw.close()

    store = TaskStore(db)
    assert store.get_person_role("saman") == "طراح محصول از kv"
    assert store.get_person_role("kawe")  # seeded known fact
    assert store.get_person_role("hamed") == "مدیرعامل، سرپرست طراحی، طراح"
    assert "hamed" not in store.list_people_missing_role()
    assert store.get_kv("telegram_group_id") == "-100"


def test_upsert_mapping_syncs_people(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "map.db")
    store.upsert_user_mapping(
        telegram_user_id=99,
        member_key="kawe",
        username="kvmmn",
        display_name="Kawe",
    )
    with store._conn() as conn:
        row = conn.execute(
            """
            SELECT pi.provider_user_id AS telegram_user_id, pi.username, p.display_name
            FROM person_identities pi
            JOIN people p ON p.id = pi.person_id
            WHERE p.slug = 'kawe' AND pi.provider = 'telegram'
            """
        ).fetchone()
    assert int(row["telegram_user_id"]) == 99
    assert row["username"] == "kvmmn"


def test_person_events_and_lessons(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "events.db")
    store.log_person_event("hamed", "role_set", payload={"value": "ceo"}, telegram_message_id=7)
    events = store.list_person_events("hamed", limit=10)
    assert events
    assert events[0]["event_type"] == "role_set"
    assert events[0]["telegram_message_id"] == 7
    store.add_lesson("همیشه فارسی صحبت کن.", source="kaveh")
    store.add_lesson("همیشه فارسی صحبت کن.", source="kaveh")
    assert "همیشه فارسی صحبت کن." in store.list_lessons()
    assert store.list_lessons().count("همیشه فارسی صحبت کن.") == 1


def test_hamed_role_never_missing(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "hamed.db")
    assert store.get_person_role("hamed") == "مدیرعامل، سرپرست طراحی، طراح"
    missing = store.list_people_missing_role()
    assert "hamed" not in missing
    assert missing == ["mohammadreza", "saman"]
