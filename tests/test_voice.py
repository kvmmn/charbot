"""Voice pipeline: persist transcript, recall later. ASR is mocked."""

from __future__ import annotations

from pathlib import Path

from charbot.store import TaskStore
from charbot.voice import (
    answer_voice_question,
    is_voice_question,
    persist_transcript,
    summarize_fa,
)


GROUP = -1002781646107
TRANSCRIPT = "ببین من یه کار دیگه هم انجام میدم و اون براورد مالی پروژه هست که در ابتدای پروژه باید انجام بشه"


def test_is_voice_question() -> None:
    assert is_voice_question("وویس راجع به چی بود")
    assert is_voice_question("اون ویس چی گفت؟")
    assert not is_voice_question("کارهای باز")
    assert not is_voice_question("نقش حامد چیه")


def test_summarize_keeps_short_text() -> None:
    assert summarize_fa(TRANSCRIPT) == TRANSCRIPT


def test_persist_and_recall_voice(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "voice.db")
    store.log_inbox(
        telegram_update_id=9001,
        chat_id=GROUP,
        chat_type="supergroup",
        chat_title="X-Chaharsotoon",
        user_id=None,
        username="samanf202",
        display_name="Saman",
        message_id=1944,
        kind="voice",
        text=None,
        file_id="file-abc",
        media_path=str(tmp_path / "1944.ogg"),
    )
    store.upsert_user_mapping(
        telegram_user_id=111,
        member_key="saman",
        username="samanf202",
        display_name="Saman",
    )
    with store._conn() as conn:
        row = conn.execute(
            "SELECT id FROM messages WHERE telegram_message_id = 1944"
        ).fetchone()
        mid = int(row["id"])
        conn.execute("UPDATE messages SET person_id = (SELECT id FROM people WHERE slug = 'saman') WHERE id = ?", (mid,))

    summary, rid = persist_transcript(
        store,
        transcript=TRANSCRIPT,
        member_key="saman",
        telegram_message_id=1944,
        chat_id=GROUP,
        message_row_id=mid,
    )
    assert rid == mid
    assert "برآورد" in summary or "براورد" in summary
    rec = store.get_latest_voice_message(chat_id=GROUP, member_key="saman", require_body=True)
    assert rec is not None
    assert rec["body"] == TRANSCRIPT
    assert store.get_person_fact("saman", "notes", "latest_voice") == TRANSCRIPT
    events = store.list_person_events("saman")
    assert any(e["event_type"] == "voice_transcribed" for e in events)
    answer = answer_voice_question(store, chat_id=GROUP)
    assert "سامان" in answer
    assert "پروژه" in answer
    assert answer_voice_question(store, chat_id=GROUP, member_key="saman").startswith("سامان")


def test_bot_handlers_are_unique() -> None:
    import inspect

    import charbot.bot as bot

    src = inspect.getsource(bot)
    assert src.count("async def cmd_open") == 1
    assert src.count("async def cmd_standup") == 1
    assert src.count("async def _reply_task_created") == 1
    assert src.count("async def handle_natural_language") == 1
    assert src.count("async def handle_media") == 1
    assert "کارهای باز ثبت شد. روی نقش گیر نمی‌کنم" not in src
