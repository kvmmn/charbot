"""Prior-message context: متوجه شدی / ذخیره کن after a work dump."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from charbot.bot import (
    ASK_WHO_TEXT,
    interpret_work_or_followup,
    load_prior_context,
)
from charbot.nlp import NLIntent, parse_natural_language
from charbot.store import TaskStore

GROUP = -1002781646107
TODAY = date(2026, 8, 31)
DUE = date(2026, 9, 2)
WORK = (
    "من باید فایل تدوین‌شده قرارداد متعلق به حامد رو بررسی کنم "
    "و اگر نیاز به اصلاح داشت به حامد بگم تا دو روز دیگه"
)
MOTAVAJEH = "متوجه شدی؟ @TheCharBot"
SAVE = "بعنوان یه کار قابل پیگیری ذخیره کن. جزییاتش رو بگو بهم (مسئول، زمان، موضوع)"
HAMED_FOLLOWUP = "الان نقشم و گفتم فهمیدی @TheCharBot"


def _store(tmp_path: Path) -> TaskStore:
    store = TaskStore(tmp_path / "context.db")
    store.upsert_user_mapping(
        telegram_user_id=42,
        member_key="kawe",
        username="kvmmn",
        display_name="Kawe",
    )
    store.upsert_user_mapping(
        telegram_user_id=84184761,
        member_key="hamed",
        username="Musketeer1985",
        display_name="Hamed Akhoundi",
    )
    store.set_person_role("hamed", "مدیرعامل، سرپرست طراحی، طراح", source="hamed")
    return store


def _log(
    store: TaskStore,
    message_id: int,
    text: str,
    user_id: int = 42,
    update_id: int | None = None,
) -> None:
    store.log_inbox(
        telegram_update_id=update_id or (10_000 + message_id),
        chat_id=GROUP,
        chat_type="supergroup",
        chat_title="X-Chaharsotoon",
        user_id=user_id,
        username="kvmmn" if user_id == 42 else "Musketeer1985",
        display_name="Kawe" if user_id == 42 else "Hamed",
        message_id=message_id,
        kind="text",
        text=text,
    )


def test_self_obligation_is_one_create_task() -> None:
    parsed = parse_natural_language(WORK, today=TODAY)
    assert parsed.intent == NLIntent.CREATE_TASK
    assert parsed.title == "بررسی فایل تدوین‌شده قرارداد حامد"
    assert parsed.due_date == DUE
    assert parsed.description is not None
    assert "اصلاح" in parsed.description
    assert "حامد" in parsed.description
    assert "بگو" in parsed.description
    assert parse_natural_language(SAVE, today=TODAY).intent == NLIntent.NONE


def test_motavajeh_shodi_uses_previous_work_dump(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _log(store, 1968, WORK)
    _log(store, 1969, MOTAVAJEH)
    prior, key = load_prior_context(
        store, chat_id=GROUP, raw=MOTAVAJEH, current_message_id=1969
    )
    assert prior == WORK
    assert key == "kawe"
    result = interpret_work_or_followup(
        store,
        chat_id=GROUP,
        raw=MOTAVAJEH,
        speaker_key="kawe",
        speaker_user_id=42,
        current_message_id=1969,
        addressed=True,
        today=TODAY,
    )
    assert result.task is not None
    assert result.created is True
    assert result.task.assignee_key == "kawe"
    assert result.task.due_date == DUE
    assert result.task.title == "بررسی فایل تدوین‌شده قرارداد حامد"
    assert result.task.description is not None
    assert "اصلاح" in result.task.description
    assert result.reply is not None
    assert ASK_WHO_TEXT not in result.reply
    assert "کاوه" in result.reply
    assert "بررسی فایل تدوین‌شده قرارداد حامد" in result.reply
    assert "2/9" in result.reply or "۲/۹" in result.reply


def test_imperative_save_task_uses_context(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _log(store, 1971, WORK)
    _log(store, 1973, SAVE)
    result = interpret_work_or_followup(
        store,
        chat_id=GROUP,
        raw=SAVE,
        speaker_key="kawe",
        speaker_user_id=42,
        current_message_id=1973,
        addressed=False,
        today=TODAY,
    )
    assert result.task is not None
    assert result.task.assignee_key == "kawe"
    assert result.task.due_date == DUE
    assert result.task.title == "بررسی فایل تدوین‌شده قرارداد حامد"
    assert ASK_WHO_TEXT not in (result.reply or "")
    assert "کاوه" in (result.reply or "")
    # second save must reuse the same task
    again = interpret_work_or_followup(
        store,
        chat_id=GROUP,
        raw=MOTAVAJEH,
        speaker_key="kawe",
        speaker_user_id=42,
        current_message_id=1974,
        addressed=True,
        today=TODAY,
    )
    assert again.task is not None
    assert again.task.id == result.task.id
    assert again.created is False


def test_reply_to_message_is_honored(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = interpret_work_or_followup(
        store,
        chat_id=GROUP,
        raw=MOTAVAJEH,
        speaker_key="kawe",
        speaker_user_id=42,
        reply_to_text=WORK,
        current_message_id=2001,
        addressed=True,
        today=TODAY,
    )
    assert result.task is not None
    assert result.task.title == "بررسی فایل تدوین‌شده قرارداد حامد"
    assert result.task.assignee_key == "kawe"


def test_no_false_role_sermon(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _log(store, 1968, WORK)
    result = interpret_work_or_followup(
        store,
        chat_id=GROUP,
        raw=MOTAVAJEH,
        speaker_key="kawe",
        speaker_user_id=42,
        current_message_id=1969,
        addressed=True,
        today=TODAY,
    )
    assert result.reply is not None
    assert ASK_WHO_TEXT not in result.reply
    assert "نقش تو" not in result.reply
    assert "منتظر یکی" not in result.reply
    assert "نقش‌تان" not in result.reply
    hamed = interpret_work_or_followup(
        store,
        chat_id=GROUP,
        raw=HAMED_FOLLOWUP,
        speaker_key="hamed",
        speaker_user_id=84184761,
        current_message_id=1970,
        addressed=True,
        today=TODAY,
    )
    assert hamed.task is None
    assert hamed.reply is None
