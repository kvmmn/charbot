"""Download, transcribe, persist, and recall Telegram voice notes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from charbot.members import member_display_fa
from charbot.store import TaskStore

logger = logging.getLogger(__name__)

_MODEL = None
_MODEL_NAME: str | None = None
VOICE_QUESTION_HINTS = ("وویس", "ویس ", "ویس", "صدا", "voice")
VOICE_ASK_HINTS = ("چی بود", "راجع به", "درباره", "چی گفت", "چیه", "چی میگفت", "چی می‌گفت")
ASR_FAIL_FA = "نتونستم صدا را بنویسم."


class AsrError(RuntimeError):
    """No ASR backend produced a transcript. User-visible Persian message."""

    def __init__(self, message: str = ASR_FAIL_FA) -> None:
        super().__init__(message)


_LOCAL_WHISPER_NAMES = {
    "tiny",
    "base",
    "small",
    "medium",
    "large",
    "large-v1",
    "large-v2",
    "large-v3",
    "distil-large-v3",
}


@dataclass
class VoiceResult:
    transcript: str
    summary: str
    reply: str
    member_key: str | None
    message_row_id: int | None


def media_dest(store: TaskStore, chat_id: int, message_id: int, kind: str) -> Path:
    ext = {"voice": "ogg", "audio": "ogg", "photo": "jpg", "document": "bin", "video": "mp4"}.get(
        kind, "bin"
    )
    return Path(store.db_path).parent / "media" / f"{chat_id}_{message_id}.{ext}"


def summarize_fa(transcript: str) -> str:
    text = re.sub(r"\s+", " ", (transcript or "").strip())
    if not text:
        return ""
    if len(text) <= 280:
        return text
    cut = text[:280]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"


def is_voice_question(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    low = raw.lower()
    has_voice = any(h in raw or h in low for h in VOICE_QUESTION_HINTS)
    if not has_voice:
        return False
    return any(h in raw or h in low for h in VOICE_ASK_HINTS) or raw.endswith("؟") or "?" in raw


def whisper_model_name() -> str:
    return os.environ.get("CHARBOT_WHISPER_MODEL", "large-v3").strip() or "large-v3"


def http_asr_credentials() -> tuple[str, str] | None:
    key = (
        os.environ.get("CHARBOT_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    ).strip()
    if not key:
        return None
    base = (os.environ.get("CHARBOT_LLM_BASE_URL") or "https://api.openai.com/v1").strip()
    return base, key


def http_asr_url(base: str) -> str:
    url = (base or "").strip().rstrip("/")
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    if url.endswith("/audio/transcriptions"):
        return url
    return url + "/audio/transcriptions"


def http_asr_model() -> str:
    explicit = (os.environ.get("CHARBOT_ASR_MODEL") or "").strip()
    if explicit:
        return explicit
    name = whisper_model_name().lower()
    if name in _LOCAL_WHISPER_NAMES:
        return "whisper-1"
    return name or "whisper-1"


def _multipart_body(
    fields: dict[str, str], file_field: str, file_path: Path
) -> tuple[bytes, str]:
    boundary = "----CharBotBoundary" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        )
        chunks.append(value.encode("utf-8") + b"\r\n")
    filename = file_path.name or "voice.ogg"
    chunks.append(f"--{boundary}\r\n".encode())
    chunks.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode()
    )
    chunks.append(b"Content-Type: audio/ogg\r\n\r\n")
    chunks.append(file_path.read_bytes())
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


def _transcribe_faster_whisper(audio: Path, language: str) -> str:
    global _MODEL, _MODEL_NAME
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("faster-whisper is not installed") from exc
    name = whisper_model_name()
    if _MODEL is None or _MODEL_NAME != name:
        logger.info("loading whisper model %s", name)
        _MODEL = WhisperModel(name, device="cpu", compute_type="int8")
        _MODEL_NAME = name
    segments, _info = _MODEL.transcribe(
        str(audio),
        language=language,
        vad_filter=False,
        beam_size=5,
        temperature=0.0,
        condition_on_previous_text=False,
        task="transcribe",
    )
    parts = [(seg.text or "").strip() for seg in segments]
    text = " ".join(p for p in parts if p).strip()
    if not text:
        raise RuntimeError("empty transcript")
    return text


def _transcribe_http(audio: Path, language: str) -> str:
    creds = http_asr_credentials()
    if not creds:
        raise RuntimeError("no HTTP ASR credentials")
    base, key = creds
    url = http_asr_url(base)
    body, boundary = _multipart_body(
        {"model": http_asr_model(), "language": language, "response_format": "json"},
        "file",
        audio,
    )
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("HTTP ASR request failed: %s", type(exc).__name__)
        raise RuntimeError("HTTP ASR request failed") from exc
    try:
        data = json.loads(raw)
        text = (data.get("text") or "").strip()
    except (json.JSONDecodeError, AttributeError, TypeError) as exc:
        raise RuntimeError("HTTP ASR bad response") from exc
    if not text:
        raise RuntimeError("empty transcript")
    return text


def transcribe_audio(path: str | Path, language: str = "fa") -> str:
    """HTTP ASR first when credentials exist, else local faster-whisper."""
    audio = Path(path)
    if not audio.is_file():
        raise FileNotFoundError(f"audio not found: {audio}")
    if http_asr_credentials():
        try:
            return _transcribe_http(audio, language)
        except Exception as http_exc:
            logger.info("HTTP ASR unavailable (%s); trying local", type(http_exc).__name__)
    try:
        return _transcribe_faster_whisper(audio, language)
    except FileNotFoundError:
        raise
    except Exception as local_exc:
        logger.warning("local ASR unavailable (%s)", type(local_exc).__name__)
        raise AsrError(ASR_FAIL_FA) from local_exc


async def download_telegram_file(bot: Any, file_id: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tg_file = await bot.get_file(file_id)
    await tg_file.download_to_drive(custom_path=str(dest))
    return dest


def persist_transcript(
    store: TaskStore,
    *,
    transcript: str,
    member_key: str | None,
    telegram_message_id: int | None,
    chat_id: int | None,
    message_row_id: int | None = None,
) -> tuple[str, int | None]:
    summary = summarize_fa(transcript)
    rid = store.update_message_body(
        transcript,
        message_id=message_row_id,
        telegram_chat_id=chat_id,
        telegram_message_id=telegram_message_id,
        processed=True,
    )
    if member_key:
        store.set_person_fact(member_key, "notes", "latest_voice", transcript, source="voice")
        store.set_person_fact(member_key, "notes", "latest_voice_summary", summary, source="voice")
        store.log_person_event(
            member_key,
            "voice_transcribed",
            payload={
                "transcript": transcript,
                "summary": summary,
                "telegram_message_id": telegram_message_id,
                "chat_id": chat_id,
            },
            telegram_message_id=telegram_message_id,
        )
    return summary, rid


def persian_voice_reply(member_key: str | None, transcript: str, summary: str) -> str:
    who = member_display_fa(member_key) if member_key else "گوینده"
    body = summary or transcript
    return f"{who} گفت: {body}"


def answer_voice_question(
    store: TaskStore,
    *,
    chat_id: int | None,
    member_key: str | None = None,
    telegram_message_id: int | None = None,
) -> str:
    rec = store.get_latest_voice_message(
        chat_id=chat_id,
        member_key=member_key,
        telegram_message_id=telegram_message_id,
        require_body=True,
    )
    if rec and rec.get("body"):
        return persian_voice_reply(rec.get("member_key"), rec["body"], summarize_fa(rec["body"]))
    if member_key:
        stored = store.get_person_fact(member_key, "notes", "latest_voice")
        if stored:
            return persian_voice_reply(member_key, stored, summarize_fa(stored))
    # last voice in the chat, even if member not named
    rec = store.get_latest_voice_message(chat_id=chat_id, require_body=True)
    if rec and rec.get("body"):
        return persian_voice_reply(rec.get("member_key"), rec["body"], summarize_fa(rec["body"]))
    return "هنوز رونوشت آن صدا را ندارم."


async def process_incoming_voice(
    *,
    store: TaskStore,
    bot: Any,
    chat_id: int,
    message_id: int,
    file_id: str | None,
    kind: str,
    member_key: str | None,
    existing_path: str | None = None,
) -> VoiceResult:
    dest = Path(existing_path) if existing_path else media_dest(store, chat_id, message_id, kind)
    if not dest.is_file():
        if not file_id:
            raise FileNotFoundError("voice file_id missing and local file absent")
        dest = await download_telegram_file(bot, file_id, dest)
    transcript = await asyncio.to_thread(transcribe_audio, dest, "fa")
    summary, rid = persist_transcript(
        store,
        transcript=transcript,
        member_key=member_key,
        telegram_message_id=message_id,
        chat_id=chat_id,
    )
    reply = persian_voice_reply(member_key, transcript, summary)
    return VoiceResult(
        transcript=transcript,
        summary=summary,
        reply=reply,
        member_key=member_key,
        message_row_id=rid,
    )
