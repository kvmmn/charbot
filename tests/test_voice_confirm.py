"""Voice must be confirmed by the speaker. Buttons follow the question, not a global menu."""

from __future__ import annotations

import inspect
from html import escape
from pathlib import Path

from charbot.buttons import question_buttons
from charbot.store import TaskStore
from charbot.voice import (
    CONFIRM_ASK_FA,
    apply_voice_callback,
    confirmation_prompt,
    handle_pending_voice_text,
    http_asr_model,
    persist_transcript,
    speaker_may_confirm,
    summarize_fa,
    voice_confirm_button_rows,
    voice_is_pending,
)

GROUP = -1002781646107
TRANSCRIPT = (
    "ببین من یه کار دیگه هم انجام میدم و اون براورد مالی پروژه هست "
    "که در ابتدای پروژه باید انجام بشه"
)
LONG = ("این یک رونوشت کامل آزمایشی است. " * 40).strip()


def _store(tmp_path: Path) -> TaskStore:
    store = TaskStore(tmp_path / "confirm.db")
    store.upsert_user_mapping(
        telegram_user_id=111,
        member_key="saman",
        username="samanf202",
        display_name="Saman",
    )
    store.upsert_user_mapping(
        telegram_user_id=84184761,
        member_key="hamed",
        username="Musketeer1985",
        display_name="Hamed",
    )
    return store


def _log_voice(store: TaskStore, message_id: int = 1944) -> None:
    store.log_inbox(
        telegram_update_id=9000 + message_id,
        chat_id=GROUP,
        chat_type="supergroup",
        chat_title="X-Chaharsotoon",
        user_id=111,
        username="samanf202",
        display_name="Saman",
        message_id=message_id,
        kind="voice",
        text=None,
        file_id="file-abc",
        media_path=None,
    )


def _persist(store: TaskStore, transcript: str = TRANSCRIPT, message_id: int = 1944) -> None:
    _log_voice(store, message_id)
    persist_transcript(
        store,
        transcript=transcript,
        member_key="saman",
        telegram_message_id=message_id,
        chat_id=GROUP,
        telegram_user_id=111,
    )


def _labels(rows: list[list[tuple[str, str]]]) -> list[str]:
    return [label for row in rows for label, _data in row]


def test_confirm_prompt_uses_full_transcript_not_summary() -> None:
    assert len(LONG) > 280
    assert summarize_fa(LONG) != LONG
    prompt = confirmation_prompt("saman", LONG)
    assert f"<blockquote>{escape(LONG)}</blockquote>" in prompt
    assert LONG in prompt
    assert "@samanf202" in prompt
    assert CONFIRM_ASK_FA in prompt
    assert "گفت:" not in prompt
    assert "گرفتم ثبت شد" not in prompt


def test_voice_confirm_keyboard_attached() -> None:
    rows = voice_confirm_button_rows(1944, transcript=TRANSCRIPT)
    labels = _labels(rows)
    data = [d for row in rows for _l, d in row]
    assert any("همین" in lab or "بود" in lab for lab in labels)
    assert any("اصلاح" in lab for lab in labels)
    assert any(d.startswith("vc:ok:1944") for d in data)
    assert any(d.startswith("vc:edit:1944") for d in data)
    assert "درسته" not in labels  # not the frozen form-label
    assert "گرفتم ثبت شد" not in " ".join(labels)


def test_buttons_differ_when_question_differs() -> None:
    voice = _labels(
        question_buttons(CONFIRM_ASK_FA, kind="vc", context=TRANSCRIPT, target_id=1944)
    )
    mashhad = _labels(
        question_buttons(
            "سامان، مشهد چه شد؟ رفتی؟",
            kind="td",
            context="سفر مشهد هفته بعد",
            target_id=12,
        )
    )
    pay = _labels(
        question_buttons(
            "واریز فاکتور شِی انجام شد؟",
            kind="td",
            context="پرداخت فاکتور مشتری",
            target_id=13,
        )
    )
    assert voice != mashhad
    assert mashhad != pay
    assert voice != pay
    assert any("مشهد" in lab or "نرفتم" in lab for lab in mashhad)
    assert any("واریز" in lab or "پرداخت" in lab or "هنوز" in lab for lab in pay)
    assert any("اصلاح" in lab for lab in voice)


def test_yes_word_locks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _persist(store)
    assert voice_is_pending(store, "saman")
    result = handle_pending_voice_text(
        store, member_key="saman", text="بله", chat_id=GROUP
    )
    assert result.action == "confirm"
    assert not voice_is_pending(store, "saman")
    events = store.list_person_events("saman")
    assert any(e["event_type"] == "voice_confirmed" for e in events)
    assert store.get_person_fact("saman", "notes", "latest_voice") == TRANSCRIPT
    assert store.list_open_tasks(GROUP) == []


def test_callback_confirm_locks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _persist(store)
    result = apply_voice_callback(store, owner_key="saman", action="ok")
    assert result.action == "confirm"
    assert result.transcript == TRANSCRIPT
    assert not voice_is_pending(store, "saman")
    events = store.list_person_events("saman")
    assert any(e["event_type"] == "voice_confirmed" for e in events)


def test_callback_edit_then_text(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _persist(store)
    wait = apply_voice_callback(store, owner_key="saman", action="edit")
    assert wait.action == "wait_edit"
    assert voice_is_pending(store, "saman")
    corrected = "براورد مالی پروژه در ابتدای کار باید انجام شود"
    result = handle_pending_voice_text(
        store, member_key="saman", text=corrected, chat_id=GROUP
    )
    assert result.action == "correct"
    assert store.get_person_fact("saman", "notes", "latest_voice") == corrected
    assert voice_is_pending(store, "saman")
    assert corrected in (result.reply or "")
    assert "blockquote" in (result.reply or "")
    events = store.list_person_events("saman")
    assert any(e["event_type"] == "voice_corrected" for e in events)
    # second confirm locks the edited text
    locked = handle_pending_voice_text(
        store, member_key="saman", text="آره", chat_id=GROUP
    )
    assert locked.action == "confirm"
    assert store.get_person_fact("saman", "notes", "latest_voice") == corrected


def test_edited_text_replaces(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _persist(store)
    result = handle_pending_voice_text(
        store,
        member_key="saman",
        text="متن درست من این است درباره برآورد مالی",
        chat_id=GROUP,
    )
    assert result.action == "correct"
    assert "برآورد" in (store.get_person_fact("saman", "notes", "latest_voice") or "")
    assert voice_is_pending(store, "saman")


def test_other_speaker_ignored(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _persist(store)
    result = handle_pending_voice_text(
        store, member_key="hamed", text="بله", chat_id=GROUP
    )
    assert result.action == "ignored"
    assert voice_is_pending(store, "saman")
    assert not speaker_may_confirm(store, "saman", "hamed", 84184761)


def test_wrong_user_callback_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _persist(store)
    assert speaker_may_confirm(store, "saman", "saman", 111)
    assert not speaker_may_confirm(store, "saman", "hamed", 84184761)


def test_new_voice_resets_pending(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _persist(store, TRANSCRIPT, 1944)
    second = "ویس دوم درباره جلسه دوشنبه"
    _persist(store, second, 2001)
    assert store.get_person_fact("saman", "notes", "latest_voice") == second
    assert store.get_person_fact("saman", "notes", "voice_pending_tg_id") == "2001"
    assert voice_is_pending(store, "saman")


def test_http_model_default(monkeypatch) -> None:
    monkeypatch.delenv("CHARBOT_ASR_MODEL", raising=False)
    monkeypatch.setenv("CHARBOT_WHISPER_MODEL", "tiny")
    assert http_asr_model() == "gpt-4o-mini-transcribe"
    monkeypatch.setenv("CHARBOT_ASR_MODEL", "whisper-1")
    assert http_asr_model() == "whisper-1"


def test_bot_hooks_confirm_and_callback() -> None:
    import charbot.bot as bot

    src = inspect.getsource(bot)
    nl = inspect.getsource(bot.handle_natural_language)
    media = inspect.getsource(bot.handle_media)
    assert src.find("handle_pending_voice_text") < src.find("interpret_work_or_followup")
    assert "handle_pending_voice_text" in nl
    assert nl.find("handle_pending_voice_text") < nl.find("if len(raw) < 4")
    assert "asyncio.create_task" in media
    assert "ParseMode.HTML" in media
    assert "reply_markup" in media
    assert "CallbackQueryHandler" in src
    assert "گرفتم ثبت شد" not in src
    assert inspect.getsource(bot.handle_callback).count("answer") >= 1
