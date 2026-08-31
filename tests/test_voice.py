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


def test_http_asr_url_and_model(monkeypatch) -> None:
    from charbot.voice import http_asr_model, http_asr_url

    assert http_asr_url("https://api.openai.com/v1") == (
        "https://api.openai.com/v1/audio/transcriptions"
    )
    assert http_asr_url("https://api.openai.com/v1/chat/completions") == (
        "https://api.openai.com/v1/audio/transcriptions"
    )
    monkeypatch.setenv("CHARBOT_WHISPER_MODEL", "tiny")
    monkeypatch.delenv("CHARBOT_ASR_MODEL", raising=False)
    assert http_asr_model() == "whisper-1"
    monkeypatch.setenv("CHARBOT_ASR_MODEL", "whisper-large-v3")
    assert http_asr_model() == "whisper-large-v3"


def test_transcribe_http_fallback(tmp_path, monkeypatch) -> None:
    import json

    from charbot import voice

    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"OggSfake")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("CHARBOT_LLM_BASE_URL", "https://api.openai.com/v1")
    local_called = {"n": 0}

    def _local(*_a, **_k):
        local_called["n"] += 1
        raise AssertionError("local ASR must not run when HTTP credentials exist")

    monkeypatch.setattr(voice, "_transcribe_faster_whisper", _local)

    captured: dict = {}

    class FakeResp:
        def read(self) -> bytes:
            return json.dumps({"text": "salam from api"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return FakeResp()

    monkeypatch.setattr(voice.urllib.request, "urlopen", fake_urlopen)
    text = voice.transcribe_audio(audio, language="fa")
    assert text == "salam from api"
    assert captured["url"].endswith("/audio/transcriptions")
    assert "test-key" not in captured["url"]
    assert local_called["n"] == 0


def test_transcribe_fails_persian_without_key(tmp_path, monkeypatch) -> None:
    from charbot import voice

    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"OggSfake")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CHARBOT_LLM_API_KEY", raising=False)
    monkeypatch.setattr(
        voice,
        "_transcribe_faster_whisper",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("faster-whisper is not installed")),
    )
    try:
        voice.transcribe_audio(audio)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert str(exc) == voice.ASR_FAIL_FA


def test_handle_media_uses_persian_asr_fail() -> None:
    import inspect

    import charbot.bot as bot

    src = inspect.getsource(bot.handle_media)
    assert "ASR_FAIL_FA" in src


def test_redact_bot_token_in_logs() -> None:
    from charbot.main import RedactSecretsFilter

    leaked = "HTTP Request: GET https://api.telegram.org/bot123456:AAHplaceholder/getFile"
    cleaned = RedactSecretsFilter.scrub(leaked)
    assert "AAHplaceholder" not in cleaned
    assert "bot<redacted>" in cleaned
    bearer = RedactSecretsFilter.scrub("Authorization: Bearer abc.def")
    assert "abc.def" not in bearer


def test_missing_key_uses_local(tmp_path, monkeypatch) -> None:
    from charbot import voice

    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"OggSfake")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CHARBOT_LLM_API_KEY", raising=False)
    monkeypatch.setattr(
        voice, "_transcribe_faster_whisper", lambda *_a, **_k: "رونوشت محلی"
    )
    assert voice.transcribe_audio(audio) == "رونوشت محلی"


def test_handle_media_backgrounds_voice() -> None:
    import inspect

    import charbot.bot as bot

    src = inspect.getsource(bot.handle_media)
    assert "asyncio.create_task" in src
    assert "_finish_voice" in src
