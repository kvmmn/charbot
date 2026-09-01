from datetime import UTC, date, datetime, timedelta

from charbot.formatting import format_task, format_task_list
from charbot.store import Task, TaskStatus


def _task(**kw):
    now = datetime.now(UTC)
    base = dict(
        id=3,
        group_id=-1,
        title="اتاق سیگار مشهد",
        description="با سرپرست کارگاه مقیم؛ جزئیات طولانی که نباید در لیست بیاید.",
        assignee_key="saman",
        due_date=None,
        status=TaskStatus.OPEN,
        created_by_user_id=None,
        created_at=now,
        updated_at=now,
        completed_at=None,
    )
    base.update(kw)
    return Task(**base)


def test_list_shows_only_title_owner_due():
    t = _task()
    text = format_task_list([t], header="کارهای باز")
    assert "اتاق سیگار مشهد" in text
    assert "سامان" in text
    assert "بدون موعد" in text
    assert "سرپرست کارگاه" not in text
    assert "description" not in text
    assert "Owner:" not in text
    assert "<blockquote>" in text


def test_due_date_compact():
    due = date.today() + timedelta(days=2)
    t = _task(due_date=due)
    text = format_task(t)
    assert f"{due.day}/{due.month}" in text
    assert "سرپرست" not in text


def test_screenshot_layout_and_person_rings():
    saman = _task(assignee_key="saman", due_date=date(2026, 9, 1))
    hamed = _task(assignee_key="hamed", due_date=date(2026, 9, 2), title="لوگو شی")
    text_s = format_task(saman)
    text_h = format_task(hamed)
    assert text_s.startswith("<blockquote>")
    assert "1/9 سامان" in text_s
    assert "🟠" in text_s
    assert "🟢" in text_h
    assert "🟠" not in text_h
    from charbot.formatting import PERSON_MARK

    assert len(set(PERSON_MARK.values())) == len(PERSON_MARK)
