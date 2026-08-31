"""Period report: Berlin week/month bounds and per-person follow-through."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from charbot.report import (
    berlin_today,
    format_period_report,
    month_bounds,
    parse_report_request,
    period_report,
    week_bounds,
)
from charbot.store import TaskStatus, TaskStore

GROUP = -1002781646107
BERLIN = ZoneInfo("Europe/Berlin")
TODAY = date(2026, 8, 31)  # Monday


def _store(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "report.db")


def _complete(store: TaskStore, task_id: int, when: datetime) -> None:
    with store._conn() as conn:
        conn.execute(
            "UPDATE tasks SET status = ?, completed_at = ?, updated_at = ? WHERE id = ?",
            ("done", when.astimezone(BERLIN).isoformat(), when.isoformat(), task_id),
        )


def test_week_and_month_bounds() -> None:
    start, end = week_bounds(TODAY)
    assert TODAY.weekday() == 0
    assert start == date(2026, 8, 31)
    assert end == date(2026, 9, 6)
    m_start, m_end = month_bounds(TODAY)
    assert m_start == date(2026, 8, 1)
    assert m_end == date(2026, 8, 31)
    # mid-week still snaps to Monday
    wed = date(2026, 9, 2)
    ws, we = week_bounds(wed)
    assert ws == date(2026, 8, 31)
    assert we == date(2026, 9, 6)
    jan = month_bounds(date(2026, 1, 15))
    assert jan == (date(2026, 1, 1), date(2026, 1, 31))


def test_parse_report_phrases() -> None:
    week = parse_report_request("گزارش این هفته", today=TODAY)
    assert week is not None
    assert (week.start, week.end) == week_bounds(TODAY)
    month = parse_report_request("گزارش ماه", today=TODAY)
    assert month is not None
    assert (month.start, month.end) == month_bounds(TODAY)
    assert parse_report_request("گزارش این ماه", today=TODAY) is not None
    spanned = parse_report_request("گزارش از 2026-08-01 تا 2026-08-10", today=TODAY)
    assert spanned is not None
    assert spanned.start == date(2026, 8, 1)
    assert spanned.end == date(2026, 8, 10)
    assert parse_report_request("کارهای باز", today=TODAY) is None


def test_per_person_counts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Saman: one on-time done this week, one late done this week, one still open overdue
    t1 = store.create_task(
        group_id=GROUP, title="لوگو", assignee_key="saman", due_date=date(2026, 9, 2)
    )
    store.mark_done(t1.id, GROUP)
    _complete(store, t1.id, datetime(2026, 8, 31, 10, 0, tzinfo=BERLIN))

    t2 = store.create_task(
        group_id=GROUP, title="فایل", assignee_key="saman", due_date=date(2026, 8, 20)
    )
    store.mark_done(t2.id, GROUP)
    _complete(store, t2.id, datetime(2026, 8, 31, 18, 0, tzinfo=BERLIN))

    store.create_task(
        group_id=GROUP, title="جلسه", assignee_key="saman", due_date=date(2026, 8, 20)
    )

    # Hamed: done last month, not this week
    old = store.create_task(
        group_id=GROUP, title="پرداخت", assignee_key="hamed", due_date=date(2026, 8, 1)
    )
    store.mark_done(old.id, GROUP)
    _complete(store, old.id, datetime(2026, 8, 5, 12, 0, tzinfo=BERLIN))
    store.create_task(group_id=GROUP, title="نمونه", assignee_key="hamed")

    # Kawe: nothing done, nothing open
    # Ghazal: one open, zero done
    store.create_task(group_id=GROUP, title="استوری", assignee_key="ghazal")

    start, end = week_bounds(TODAY)
    rows = {r.slug: r for r in period_report(store, GROUP, start, end, today=TODAY)}
    assert set(rows) == {"kawe", "hamed", "saman", "mohammadreza", "ghazal"}
    saman = rows["saman"]
    assert saman.done_in_period == 2
    assert saman.on_time_done == 1
    assert saman.late_done == 1
    assert saman.still_open == 1
    assert saman.overdue == 1
    hamed = rows["hamed"]
    assert hamed.done_in_period == 0  # completed 5 Aug, outside this week
    assert hamed.still_open == 1
    assert rows["ghazal"].still_open == 1
    assert rows["ghazal"].done_in_period == 0
    assert rows["kawe"].done_in_period == 0
    assert rows["kawe"].still_open == 0

    month_rows = {
        r.slug: r
        for r in period_report(store, GROUP, *month_bounds(TODAY), today=TODAY)
    }
    assert month_rows["hamed"].done_in_period == 1
    assert month_rows["saman"].done_in_period == 2

    html = format_period_report(list(rows.values()), start, end, label="این هفته")
    assert "بازه" in html
    assert "سامان" in html
    assert "<blockquote>" in html
    assert "غزل" in html
    assert "کم‌کاری احتمالی" in html
    assert "گرفتم ثبت شد" not in html


def test_mark_done_sets_completed_at_and_events(tmp_path: Path) -> None:
    store = _store(tmp_path)
    task = store.create_task(group_id=GROUP, title="بستن کار", assignee_key="saman")
    assert task.completed_at is None
    assigned = store.list_task_events(task.id)
    assert any(e["event_type"] == "assigned" for e in assigned)
    done = store.mark_done(task.id, GROUP, actor_key="saman")
    assert done is not None
    assert done.status == TaskStatus.DONE
    assert done.completed_at is not None
    events = store.list_task_events(task.id)
    assert any(e["event_type"] == "done" for e in events)
    reopened = store.set_status(task.id, GROUP, TaskStatus.OPEN, actor_key="kawe")
    assert reopened is not None
    assert reopened.status == TaskStatus.OPEN
    assert reopened.completed_at is None
    events2 = store.list_task_events(task.id)
    assert any(e["event_type"] == "reopened" for e in events2)
