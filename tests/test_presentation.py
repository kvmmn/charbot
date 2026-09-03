"""Bot-level presentation contract: digest -> one active card -> resolved edit.

Uses lightweight fakes for python-telegram-bot objects (no network, no real
Telegram client) so the wiring in charbot/bot.py can be exercised directly.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.constants import ChatType

import charbot.bot as bot
from charbot.config import Settings
from charbot.store import TaskStore

GROUP = -1002781646107
TODAY = date(2026, 9, 3)


# ---------------------------------------------------------------------------
# Minimal python-telegram-bot fakes
# ---------------------------------------------------------------------------


class FakeUser:
    def __init__(self, id=1, username="kvmmn", full_name="Kaveh", first_name="Kaveh"):
        self.id = id
        self.username = username
        self.full_name = full_name
        self.first_name = first_name
        self.is_bot = False


class FakeChat:
    def __init__(self, chat_id=GROUP, chat_type=ChatType.SUPERGROUP):
        self.id = chat_id
        self.type = chat_type
        self.title = "X-Chaharsotoon"
        self.full_name = None


class FakeMessage:
    def __init__(self, text=None, chat_id=GROUP, message_id=1, reply_to_message=None):
        self.text = text
        self.caption = None
        self.chat_id = chat_id
        self.message_id = message_id
        self.reply_to_message = reply_to_message
        self.entities = []
        self.voice = self.audio = self.photo = self.document = self.video = None
        self.reply_text = AsyncMock()


class FakeUpdate:
    def __init__(self, message=None, user=None, chat=None, callback_query=None):
        self.effective_message = message
        self.effective_user = user or FakeUser()
        self.effective_chat = chat or FakeChat()
        self.callback_query = callback_query
        self.update_id = 1


class FakeButton:
    def __init__(self, text, callback_data):
        self.text = text
        self.callback_data = callback_data


class FakeMarkup:
    def __init__(self, inline_keyboard):
        self.inline_keyboard = inline_keyboard


class FakeQueryMessage:
    def __init__(self, chat_id=GROUP, message_id=1, reply_markup=None):
        self.chat_id = chat_id
        self.message_id = message_id
        self.reply_markup = reply_markup
        self.reply_text = AsyncMock()


class FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answer = AsyncMock()
        self.edit_message_text = AsyncMock()
        self.edit_message_reply_markup = AsyncMock()


class FakeContext:
    def __init__(self, store, settings, fake_bot=None):
        self.bot_data = {"store": store, "settings": settings}
        self.bot = fake_bot or MagicMock(send_message=AsyncMock())
        self.args = []


def _settings(**overrides) -> Settings:
    kw = dict(
        telegram_bot_token="test-token",
        telegram_group_id=None,
        telegram_allowed_group_ids=str(GROUP),
        followup_enabled=True,
        followup_interval_hours=24,
    )
    kw.update(overrides)
    return Settings(**kw)


def _store(tmp_path: Path) -> TaskStore:
    store = TaskStore(tmp_path / "presentation.db")
    store.upsert_user_mapping(
        telegram_user_id=1, member_key="kawe", username="kvmmn", display_name="Kaveh"
    )
    return store


def _real_overdue_tasks(store: TaskStore):
    specs = [
        ("اجرای سه لوگو", "ghazal", date(2026, 8, 29)),
        ("صورتجلسه هیئت مدیره", "hamed", date(2026, 8, 30)),
        ("قیمت فیلم‌بردار اینستاگرام", "ghazal", date(2026, 9, 1)),
        ("جلسه سه‌شنبه", "mohammadreza", date(2026, 9, 1)),
        ("بلیط پرواز مشهد", "saman", date(2026, 9, 1)),
    ]
    return [
        store.create_task(group_id=GROUP, title=title, assignee_key=who, due_date=due)
        for title, who, due in specs
    ]


def _all_callback_data(markup) -> list[str]:
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


# ---------------------------------------------------------------------------
# followup_job: digest -> single active card -> queue (4+ candidates)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_followup_sends_digest_then_one_active_card_and_queues_rest(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(bot, "FOLLOWUP_SEND_DELAY", 0)
    store = _store(tmp_path)
    tasks = _real_overdue_tasks(store)
    fake_bot = MagicMock(send_message=AsyncMock())

    await bot._run_followup_for_group(fake_bot, store, GROUP)

    assert fake_bot.send_message.call_count == 2
    digest_call, card_call = fake_bot.send_message.call_args_list
    digest_text = digest_call.kwargs["text"]
    assert digest_text.startswith("<b>کارهای عقب‌افتاده</b>")
    assert "reply_markup" not in digest_call.kwargs or digest_call.kwargs["reply_markup"] is None
    assert "غزل" in digest_text and "حامد" in digest_text and "سامان" in digest_text

    card_text = card_call.kwargs["text"]
    assert card_text.startswith("<b>پاسخ لازم</b>")
    markup = card_call.kwargs["reply_markup"]
    most_urgent = tasks[0]  # Ghazal's 29 Aug logo task is the oldest overdue
    ids_in_markup = {int(cd.split(":")[-1]) for cd in _all_callback_data(markup)}
    assert ids_in_markup == {most_urgent.id}  # exactly THAT task's callback ids

    # The remaining 4 tasks are queued for sequential delivery, not dumped.
    queued = json.loads(store.get_kv(bot._followup_queue_key(GROUP)))
    assert len(queued) == 4
    assert most_urgent.id not in queued


@pytest.mark.asyncio
async def test_followup_notes_how_many_remain_past_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "FOLLOWUP_SEND_DELAY", 0)
    monkeypatch.setattr(bot, "FOLLOWUP_MAX_CANDIDATES", 2)
    store = _store(tmp_path)
    _real_overdue_tasks(store)  # 5 tasks, cap is patched down to 2
    fake_bot = MagicMock(send_message=AsyncMock())

    await bot._run_followup_for_group(fake_bot, store, GROUP)

    # digest + one active card (2 candidates, distinct people -> would still
    # be sequential since burst limit default is 3 but candidates=2 distinct
    # people so it bursts) + a trailing "N more" read-only note.
    calls = fake_bot.send_message.call_args_list
    last_text = calls[-1].kwargs.get("text") or calls[-1].args[0]
    assert "کار دیگر در نوبت است" in last_text
    assert "۳" in last_text  # 5 total - 2 candidates = 3 left out entirely
    assert "reply_markup" not in calls[-1].kwargs or calls[-1].kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_followup_bursts_when_few_distinct_people(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "FOLLOWUP_SEND_DELAY", 0)
    store = _store(tmp_path)
    store.create_task(
        group_id=GROUP, title="کار غزل", assignee_key="ghazal", due_date=date(2026, 8, 29)
    )
    store.create_task(
        group_id=GROUP, title="کار حامد", assignee_key="hamed", due_date=date(2026, 8, 30)
    )
    fake_bot = MagicMock(send_message=AsyncMock())

    await bot._run_followup_for_group(fake_bot, store, GROUP)

    # digest + 2 active cards (different named people) sent in the same run
    assert fake_bot.send_message.call_count == 3
    for call in fake_bot.send_message.call_args_list[1:]:
        assert call.kwargs["text"].startswith("<b>پاسخ لازم</b>")
    assert store.get_kv(bot._followup_queue_key(GROUP)) in (None, "", "[]")


@pytest.mark.asyncio
async def test_followup_job_noop_when_disabled(tmp_path):
    store = _store(tmp_path)
    _real_overdue_tasks(store)
    settings = _settings(followup_enabled=False)
    fake_bot = MagicMock(send_message=AsyncMock())
    context = FakeContext(store, settings, fake_bot)

    await bot.followup_job(context)

    fake_bot.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# handle_callback: resolved edit + advance to next queued card
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_resolves_message_and_advances_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "FOLLOWUP_SEND_DELAY", 0)
    store = _store(tmp_path)
    tasks = _real_overdue_tasks(store)
    ghazal_task = tasks[0]
    hamed_task = tasks[1]
    bot._save_followup_queue(store, GROUP, [hamed_task.id])

    ask = bot._followup_ask_text(ghazal_task)
    rows = bot.question_buttons(ask, kind="fu", context=ghazal_task.title, target_id=ghazal_task.id)
    tapped_data = rows[0][0][1]
    tapped_label = rows[0][0][0]
    markup = FakeMarkup(
        [[FakeButton(label, data) for label, data in row] for row in rows]
    )
    query_message = FakeQueryMessage(chat_id=GROUP, reply_markup=markup)
    query = FakeQuery(tapped_data, query_message)
    fake_bot = MagicMock(send_message=AsyncMock())
    settings = _settings()
    context = FakeContext(store, settings, fake_bot)
    update = FakeUpdate(callback_query=query, user=FakeUser())

    await bot.handle_callback(update, context)

    query.edit_message_text.assert_awaited_once()
    resolved_text = query.edit_message_text.call_args.args[0]
    assert resolved_text.startswith("<b>ثبت شد</b>")
    assert f": {tapped_label}" in resolved_text
    assert query.edit_message_text.call_args.kwargs["reply_markup"] is None

    # Advanced: the queued Hamed task was sent as the next active card.
    fake_bot.send_message.assert_awaited_once()
    next_text = fake_bot.send_message.call_args.kwargs["text"]
    assert next_text.startswith("<b>پاسخ لازم</b>")
    next_markup = fake_bot.send_message.call_args.kwargs["reply_markup"]
    next_ids = {int(cd.split(":")[-1]) for cd in _all_callback_data(next_markup)}
    assert next_ids == {hamed_task.id}

    # Queue is now empty.
    assert json.loads(store.get_kv(bot._followup_queue_key(GROUP))) == []


@pytest.mark.asyncio
async def test_callback_done_choice_marks_task_done(tmp_path):
    store = _store(tmp_path)
    tasks = _real_overdue_tasks(store)
    task = tasks[0]
    rows = bot.question_buttons(
        bot._followup_ask_text(task), kind="fu", context=task.title, target_id=task.id
    )
    # find the "done"-meaning button
    done_pair = next(
        (label, data)
        for row in rows
        for label, data in row
        if bot.choice_means_done(data.split(":")[1])
    )
    markup = FakeMarkup([[FakeButton(*done_pair)]])
    query = FakeQuery(done_pair[1], FakeQueryMessage(chat_id=GROUP, reply_markup=markup))
    context = FakeContext(store, _settings())
    update = FakeUpdate(callback_query=query, user=FakeUser())

    await bot.handle_callback(update, context)

    refreshed = store.get_task(task.id, GROUP)
    assert refreshed.status.value == "done"


# ---------------------------------------------------------------------------
# Read-only surfaces never carry a keyboard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cmd_open_list_has_no_buttons(tmp_path):
    store = _store(tmp_path)
    _real_overdue_tasks(store)
    context = FakeContext(store, _settings())
    message = FakeMessage()
    update = FakeUpdate(message=message)

    await bot.cmd_open(update, context)

    message.reply_text.assert_awaited_once()
    kwargs = message.reply_text.call_args.kwargs
    assert kwargs.get("reply_markup") is None
    text = message.reply_text.call_args.args[0]
    assert text.startswith("<b>کارهای باز</b>")


@pytest.mark.asyncio
async def test_cmd_standup_plan_is_read_only(tmp_path):
    store = _store(tmp_path)
    _real_overdue_tasks(store)  # 5 decisions pending -> no auto active card
    context = FakeContext(store, _settings())
    message = FakeMessage()
    update = FakeUpdate(message=message)

    await bot.cmd_standup(update, context)

    message.reply_text.assert_awaited_once()
    kwargs = message.reply_text.call_args.kwargs
    assert kwargs.get("reply_markup") is None
    text = message.reply_text.call_args.args[0]
    assert text.startswith("<b>برنامهٔ امروز</b>")
    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_cmd_standup_sends_one_active_card_for_single_decision(tmp_path):
    store = _store(tmp_path)
    store.create_task(
        group_id=GROUP, title="تنها کار عقب‌افتاده", assignee_key="saman", due_date=date(2026, 8, 29)
    )
    context = FakeContext(store, _settings())
    message = FakeMessage()
    update = FakeUpdate(message=message)

    await bot.cmd_standup(update, context)

    message.reply_text.assert_awaited_once()
    context.bot.send_message.assert_awaited_once()
    card_text = context.bot.send_message.call_args.kwargs["text"]
    assert card_text.startswith("<b>پاسخ لازم</b>")


# ---------------------------------------------------------------------------
# ASK_WHICH: list and question are two separate messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_which_sends_list_then_separate_question_message(tmp_path):
    store = _store(tmp_path)
    store.create_task(
        group_id=GROUP, title="ویدیوی اینستاگرام", assignee_key="ghazal", due_date=date(2026, 9, 1)
    )
    context = FakeContext(store, _settings())
    message = FakeMessage(text="غزل چیکار می‌کنه؟")
    update = FakeUpdate(message=message)

    await bot.handle_natural_language(update, context)

    assert message.reply_text.await_count == 2
    list_call, question_call = message.reply_text.call_args_list
    list_text = list_call.args[0]
    assert "ویدیوی اینستاگرام" in list_text
    assert list_call.kwargs.get("reply_markup") is None
    assert question_call.kwargs.get("reply_markup") is not None


# ---------------------------------------------------------------------------
# task_pick_buttons carry their task number (no bare "#12" anchor)
# ---------------------------------------------------------------------------


def test_task_pick_buttons_label_carries_task_number():
    rows = bot.task_pick_buttons([("بررسی قرارداد", 12), ("تماس با مشتری", 7)])
    labels = [label for row in rows for label, _data in row]
    assert any("کار ۱۲" in lab and "بررسی قرارداد" in lab for lab in labels)
    assert not any(lab.strip().startswith("#") for lab in labels)
