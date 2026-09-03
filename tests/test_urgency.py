"""Urgency job: three absolute bands, ordered lists, one active card."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from charbot.formatting import _due_sentence, format_task_question
from charbot.jobs.common import JobMessage
from charbot.jobs.followup import ask_for_task
from charbot.jobs.urgency import (
    BUCKET_HEADERS,
    MODE_DUE_TODAY,
    MODE_DUE_TOMORROW,
    MODE_OVERDUE,
    card_candidates,
    run,
    split_buckets,
)
from charbot.store import Task, TaskStatus, TaskStore

GROUP = -1002781646107
TODAY = date(2026, 9, 3)
BERLIN = ZoneInfo("Europe/Berlin")


class FakeSender:
    def __init__(self) -> None:
        self.messages: list[JobMessage] = []

    async def send(self, message: JobMessage):
        self.messages.append(message)
        return len(self.messages)


def _store(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "urgency.db")


def _task(
    *,
    id: int = 1,
    title: str = "کار",
    assignee_key: str | None = "saman",
    due_date: date | None = None,
) -> Task:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=BERLIN)
    return Task(
        id=id,
        group_id=GROUP,
        title=title,
        description=None,
        assignee_key=assignee_key,
        due_date=due_date,
        status=TaskStatus.OPEN,
        created_by_user_id=None,
        created_at=now,
        updated_at=now,
        completed_at=None,
    )


def test_split_buckets_by_absolute_calendar_day():
    tasks = [
        _task(id=1, due_date=TODAY - timedelta(days=2), title="دیر"),
        _task(id=2, due_date=TODAY, title="امروز"),
        _task(id=3, due_date=TODAY + timedelta(days=1), title="فردا"),
        _task(id=4, due_date=TODAY + timedelta(days=5), title="بعد"),
        _task(id=5, due_date=None, title="بدون موعد"),
    ]
    buckets = split_buckets(tasks, TODAY)
    assert [t.title for t in buckets[MODE_OVERDUE]] == ["دیر"]
    assert [t.title for t in buckets[MODE_DUE_TODAY]] == ["امروز"]
    assert [t.title for t in buckets[MODE_DUE_TOMORROW]] == ["فردا"]
    assert BUCKET_HEADERS[MODE_OVERDUE] == "عقب‌افتاده"
    assert BUCKET_HEADERS[MODE_DUE_TODAY] == "موعد امروز"
    assert BUCKET_HEADERS[MODE_DUE_TOMORROW] == "موعد فردا"


def test_due_sentence_bands_and_late_afternoon_meta():
    overdue = TODAY - timedelta(days=5)
    assert _due_sentence(overdue, TODAY) == "موعد ۷ شهریور، ۵ روز عقب‌افتاده"
    morning = datetime(2026, 9, 3, 10, 0, tzinfo=BERLIN)
    assert _due_sentence(TODAY, TODAY, now=morning) == "موعد امروز"
    evening = datetime(2026, 9, 3, 16, 30, tzinfo=BERLIN)
    assert _due_sentence(TODAY, TODAY, now=evening) == "موعد امروز، هنوز مانده"
    tomorrow = TODAY + timedelta(days=1)
    assert _due_sentence(tomorrow, TODAY) == "موعد فردا"
    assert "۰ روز" not in _due_sentence(tomorrow, TODAY)


def test_chase_question_addresses_hamed_about_ghazal():
    t = _task(assignee_key="ghazal", title="اجرای سه لوگو", due_date=overdue_date())
    ask = ask_for_task(t)
    assert ask.startswith("حامد، از غزل بپرس:")
    assert "اجرای سه لوگو" in ask
    text = format_task_question(t, ask, today=TODAY, now=datetime(2026, 9, 3, 11, tzinfo=BERLIN))
    assert "حامد، از غزل بپرس:" in text
    assert text.startswith("<b>پاسخ لازم</b>")
    # Type label for the band is separate; question carries chase.
    assert "در حال دیر شدن" not in text


def overdue_date() -> date:
    return date(2026, 8, 29)


@pytest.mark.asyncio
async def test_empty_buckets_send_nothing(tmp_path):
    store = _store(tmp_path)
    store.create_task(
        group_id=GROUP, title="آینده", assignee_key="saman", due_date=TODAY + timedelta(days=10)
    )
    sender = FakeSender()
    sent = await run(store, sender, GROUP, today=TODAY, send_delay=0)
    assert sent == []
    assert sender.messages == []


@pytest.mark.asyncio
async def test_message_ordering_and_tomorrow_has_no_active_card(tmp_path):
    store = _store(tmp_path)
    store.create_task(
        group_id=GROUP,
        title="لوگو عقب",
        assignee_key="ghazal",
        due_date=TODAY - timedelta(days=2),
    )
    store.create_task(
        group_id=GROUP,
        title="صورتجلسه امروز",
        assignee_key="hamed",
        due_date=TODAY,
    )
    store.create_task(
        group_id=GROUP,
        title="بلیط فردا",
        assignee_key="saman",
        due_date=TODAY + timedelta(days=1),
    )
    # Unowned with due still listed.
    store.create_task(
        group_id=GROUP, title="بی‌صاحب امروز", assignee_key=None, due_date=TODAY
    )
    sender = FakeSender()
    sent = await run(store, sender, GROUP, today=TODAY, send_delay=0, max_cards=5)

    texts = [m.text for m in sent]
    # Band intros in order; empty bands omitted.
    overdue_intro_i = next(i for i, t in enumerate(texts) if t.startswith("<b>عقب‌افتاده</b>"))
    today_intro_i = next(i for i, t in enumerate(texts) if t.startswith("<b>موعد امروز</b>"))
    tomorrow_intro_i = next(i for i, t in enumerate(texts) if t.startswith("<b>موعد فردا</b>"))
    assert overdue_intro_i < today_intro_i < tomorrow_intro_i
    assert "۱ کار برای ۱ نفر" in texts[overdue_intro_i]
    assert "۲ کار" in texts[today_intro_i]

    cards = [m for m in sent if m.text.startswith("<b>پاسخ لازم</b>")]
    assert len(cards) == 1
    assert cards[0].keyboard is not None
    # Tomorrow is heads-up only — no card about بلیط فردا.
    assert "بلیط فردا" not in cards[0].text
    # Overdue card first (Ghazal via Hamed).
    assert "از غزل بپرس" in cards[0].text or "لوگو عقب" in cards[0].text
    assert "حامد" in cards[0].text

    # No merged mega-digest.
    assert not any("فوری" in t for t in texts)
    assert not any(t.startswith("<b>عقب‌افتاده</b>") and "موعد امروز" in t for t in texts)


@pytest.mark.asyncio
async def test_overdue_cards_use_chase_via_and_queue_rest(tmp_path):
    store = _store(tmp_path)
    g1 = store.create_task(
        group_id=GROUP,
        title="کار غزل یک",
        assignee_key="ghazal",
        due_date=TODAY - timedelta(days=3),
    )
    store.create_task(
        group_id=GROUP,
        title="کار غزل دو",
        assignee_key="ghazal",
        due_date=TODAY - timedelta(days=1),
    )
    store.create_task(
        group_id=GROUP, title="کار سامان", assignee_key="saman", due_date=TODAY
    )
    sender = FakeSender()
    sent = await run(store, sender, GROUP, today=TODAY, send_delay=0, max_cards=5)
    cards = [m for m in sent if m.keyboard]
    assert len(cards) == 1
    assert "از غزل بپرس" in cards[0].text
    queued_raw = store.get_kv(f"followup_queue:{GROUP}")
    assert queued_raw is not None
    import json

    queued = json.loads(queued_raw)
    assert g1.id not in queued
    assert len(queued) == 2  # other overdue + today, one in flight


def test_card_candidates_skip_tomorrow_and_prefer_overdue():
    overdue = [
        _task(id=1, assignee_key="saman", due_date=TODAY - timedelta(days=2), title="ا۱"),
        _task(id=2, assignee_key="hamed", due_date=TODAY - timedelta(days=1), title="ا۲"),
    ]
    today = [_task(id=3, assignee_key="saman", due_date=TODAY, title="ا۳")]
    tomorrow = [_task(id=4, assignee_key="saman", due_date=TODAY + timedelta(days=1), title="ا۴")]
    buckets = {
        MODE_OVERDUE: overdue,
        MODE_DUE_TODAY: today,
        MODE_DUE_TOMORROW: tomorrow,
    }
    cands = card_candidates(
        buckets,
        today=TODAY,
        modes=(MODE_OVERDUE, MODE_DUE_TODAY, MODE_DUE_TOMORROW),
        max_cards=5,
    )
    assert [t.title for t in cands] == ["ا۱", "ا۲", "ا۳"]


@pytest.mark.asyncio
async def test_modes_subset_skips_other_bands(tmp_path):
    store = _store(tmp_path)
    store.create_task(
        group_id=GROUP, title="دیر", assignee_key="saman", due_date=TODAY - timedelta(days=1)
    )
    store.create_task(
        group_id=GROUP, title="فردا", assignee_key="hamed", due_date=TODAY + timedelta(days=1)
    )
    sender = FakeSender()
    sent = await run(
        store,
        sender,
        GROUP,
        today=TODAY,
        modes=(MODE_DUE_TOMORROW,),
        send_delay=0,
    )
    texts = [m.text for m in sent]
    assert any(t.startswith("<b>موعد فردا</b>") for t in texts)
    assert not any(t.startswith("<b>عقب‌افتاده</b>") for t in texts)
    assert not any(t.startswith("<b>پاسخ لازم</b>") for t in texts)
