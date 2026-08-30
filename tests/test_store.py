"""Tests for SQLite task store."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from charbot.store import TaskStatus, TaskStore


@pytest.fixture
def store(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "test.db")


GROUP = -1001234567890


def test_create_and_get_task(store: TaskStore) -> None:
    task = store.create_task(group_id=GROUP, title="Ship invoice", assignee_key="kawe")
    assert task.id == 1
    assert task.title == "Ship invoice"
    assert task.assignee_key == "kawe"
    assert task.status == TaskStatus.OPEN

    fetched = store.get_task(task.id, GROUP)
    assert fetched is not None
    assert fetched.title == "Ship invoice"


def test_assign_and_due(store: TaskStore) -> None:
    task = store.create_task(group_id=GROUP, title="Review contract")
    due = date.today() + timedelta(days=3)
    store.assign_task(task.id, GROUP, "hamed")
    updated = store.set_due_date(task.id, GROUP, due)
    assert updated is not None
    assert updated.assignee_key == "hamed"
    assert updated.due_date == due


def test_mark_done(store: TaskStore) -> None:
    task = store.create_task(group_id=GROUP, title="Close deal")
    done = store.mark_done(task.id, GROUP)
    assert done is not None
    assert done.status == TaskStatus.DONE
    assert done.completed_at is not None
    assert store.list_open_tasks(GROUP) == []


def test_list_overdue(store: TaskStore) -> None:
    task = store.create_task(group_id=GROUP, title="Late item")
    store.set_due_date(task.id, GROUP, date.today() - timedelta(days=1))
    overdue = store.list_overdue_tasks(GROUP, today=date.today())
    assert len(overdue) == 1
    assert overdue[0].id == task.id


def test_unowned_tasks(store: TaskStore) -> None:
    t1 = store.create_task(group_id=GROUP, title="No owner")
    store.create_task(group_id=GROUP, title="Has owner", assignee_key="saman")
    unowned = store.list_unowned_open_tasks(GROUP)
    assert len(unowned) == 1
    assert unowned[0].id == t1.id


def test_user_mapping(store: TaskStore) -> None:
    store.upsert_user_mapping(
        telegram_user_id=42,
        member_key="kawe",
        username="kawe_dev",
        display_name="Kawe",
    )
    m = store.get_user_mapping(42)
    assert m is not None
    assert m.member_key == "kawe"
    store.upsert_user_mapping(telegram_user_id=42, member_key="hamed")
    m2 = store.get_user_mapping(42)
    assert m2 is not None
    assert m2.member_key == "hamed"


def test_persistence(tmp_path: Path) -> None:
    db = tmp_path / "persist.db"
    s1 = TaskStore(db)
    task = s1.create_task(group_id=GROUP, title="Persist me")
    s2 = TaskStore(db)
    loaded = s2.get_task(task.id, GROUP)
    assert loaded is not None
    assert loaded.title == "Persist me"


def test_journal_mode_wal(store: TaskStore) -> None:
    with store._conn() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"
