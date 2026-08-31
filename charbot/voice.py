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
from html import escape
from pathlib import Path
from typing import Any

from charbot.buttons import question_buttons
from charbot.glossary import is_learn_utterance
from charbot.members import member_display_fa
from charbot.store import TaskStore

logger = logging.getLogger(__name__)

_MODEL = None
_MODEL_NAME: str | None = None
VOICE_QUESTION_HINTS = ("وویس", "ویس ", "ویس", "صدا", "voice")
VOICE_ASK_HINTS = ("چی بود", "راجع به", "درباره", "چی گفت", "چیه", "چی میگفت", "چی می‌گفت")
ASR_FAIL_FA = "نتونستم صدا را بنویسم."
DEFAULT_HTTP_ASR_MODEL = "gpt-4o-mini-transcribe"
OPENROUTER_ASR_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_ASR_MODEL = "deepgram/nova-3"
OPENROUTER_ASR_FALLBACK_MODEL = "openai/whisper-large-v3"
ASR_GLOSSARY_PROMPT = (
    "Persian speech. Proper names: چهارستون، شی SHEY، مشهد، فرجی، فرهمند، "
    "JTI جی‌تی‌آی، امام خمینی، مهرآباد، غزل، حامد، سامان، محمدرضا، کاوه."
)
CONFIRM_ASK_FA = "این را از صدایت نوشتم. همین بود؟"
REASK_FA = "این درست شد؟"
LOCKED_FA = "باشه، نوشتم."
FACT_PENDING = "voice_pending_confirm"
FACT_PENDING_TG = "voice_pending_tg_id"
FACT_PENDING_MSG = "voice_pending_confirm_msg_id"
FACT_PENDING_CHAT = "voice_pending_chat_id"
FACT_PENDING_EDIT = "voice_pending_edit"
FACT_PENDING_USER = "voice_pending_user_id"
FACT_LATEST = "latest_voice"
FACT_LATEST_SUMMARY = "latest_voice_summary"
EDIT_WAIT_FA = "بگو درستش چی بود، همان را می‌نویسم."
CONFIRMED_MARK_FA = "باشه، نوشتم."
WRONG_SPEAKER_FA = "این تأیید مال گوینده است"

KNOWN_USERNAMES = {
    "saman": "samanf202",
    "kawe": "kvmmn",
    "hamed": "Musketeer1985",
    "mohammadreza": "MREZA_HEIDARI08",
}

# Meaning + obvious yes-words. Matching strips filler; leftover must be tiny.
_YES_TOKENS = (
    "بله همین",
    "آره همین",
    "اره همین",
    "بله درسته",
    "آره درسته",
    "اره درسته",
    "درست است",
    "تایید میکنم",
    "تایید می‌کنم",
    "تأیید میکنم",
    "تأیید می‌کنم",
    "دقیقا همین",
    "دقیقاً همین",
    "همین را گفتم",
    "همین رو گفتم",
    "همینو گفتم",
    "همین گفتم",
    "بله",
    "آره",
    "اره",
    "آری",
    "درسته",
    "همین",
    "اوکی",
    "اکی",
    "okay",
    "ok",
    "yes",
    "yeah",
    "yep",
    "دقیقاً",
    "دقیقا",
    "تایید",
    "تأیید",
)
_INLINE_LOCK_RE = re.compile(
    r"(این درسته|این درست است|این همان است|همین درسته|این همینه)",
)
_CONFIRM_STRIP_RE = re.compile(
    r"@\S+|چاربات|thecharbot|[؟!?.!,،؛:…«»()\[\]]+",
    re.IGNORECASE,
)

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
    awaiting_confirm: bool = True
    voice_message_id: int | None = None


@dataclass
class VoiceConfirmResult:
    action: str  # ignored | confirm | correct | correct_and_lock | wait_edit
    reply: str = ""
    transcript: str | None = None
    html: bool = True
    confirm_message_id: int | None = None
    voice_tg_id: int | None = None
    member_key: str | None = None
    chat_id: int | None = None


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


@dataclass(frozen=True)
class AsrBackend:
    name: str
    base_url: str
    api_key: str
    model: str


def _openai_key() -> str:
    return (
        os.environ.get("CHARBOT_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    ).strip()


def http_asr_url(base: str) -> str:
    url = (base or "").strip().rstrip("/")
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    if url.endswith("/audio/transcriptions"):
        return url
    return url + "/audio/transcriptions"


def http_asr_model() -> str:
    """OpenAI last-fallback model. CHARBOT_ASR_MODEL wins unless it is an OpenRouter slug.

    Local faster-whisper names (tiny/base/...) map to gpt-4o-mini-transcribe, not whisper-1.
    """
    explicit = (os.environ.get("CHARBOT_ASR_MODEL") or "").strip()
    if explicit and "/" not in explicit:
        return explicit
    return DEFAULT_HTTP_ASR_MODEL


def asr_backends() -> list[AsrBackend]:
    """Ordered HTTP ASR backends. OpenRouter first when keyed; OpenAI last. No Groq path."""
    backends: list[AsrBackend] = []
    or_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    explicit = (os.environ.get("CHARBOT_ASR_MODEL") or "").strip()
    if or_key:
        first = explicit if "/" in explicit else OPENROUTER_ASR_MODEL
        backends.append(
            AsrBackend("openrouter", OPENROUTER_ASR_BASE, or_key, first)
        )
        if first != OPENROUTER_ASR_FALLBACK_MODEL:
            backends.append(
                AsrBackend(
                    "openrouter",
                    OPENROUTER_ASR_BASE,
                    or_key,
                    OPENROUTER_ASR_FALLBACK_MODEL,
                )
            )
    openai_key = _openai_key()
    if openai_key:
        base = (os.environ.get("CHARBOT_LLM_BASE_URL") or "https://api.openai.com/v1").strip()
        if "openrouter.ai" in base.lower() or "groq.com" in base.lower():
            base = "https://api.openai.com/v1"
        backends.append(
            AsrBackend("openai", base, openai_key, http_asr_model())
        )
    return backends


def http_asr_credentials() -> tuple[str, str] | None:
    backends = asr_backends()
    if not backends:
        return None
    b = backends[0]
    return b.base_url, b.api_key


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


def _transcribe_http_once(audio: Path, language: str, backend: AsrBackend) -> str:
    url = http_asr_url(backend.base_url)
    fields = {
        "model": backend.model,
        "language": language or "fa",
        "response_format": "json",
    }
    # Whisper-family models take a glossary prompt. Deepgram Nova-3 rejects it.
    if "whisper" in backend.model or backend.model.startswith("openai/gpt"):
        fields["prompt"] = ASR_GLOSSARY_PROMPT
    body, boundary = _multipart_body(fields, "file", audio)
    headers = {
        "Authorization": "Bearer " + backend.api_key,
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    if backend.name == "openrouter":
        headers["HTTP-Referer"] = "https://charbot.chaharsotoon"
        headers["X-Title"] = "charbot"
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            exc.read()
        except Exception:
            pass
        logger.warning("HTTP ASR %s failed: %s", backend.name, type(exc).__name__)
        raise RuntimeError("HTTP ASR request failed") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("HTTP ASR %s failed: %s", backend.name, type(exc).__name__)
        raise RuntimeError("HTTP ASR request failed") from exc
    try:
        data = json.loads(raw)
        text = (data.get("text") or "").strip()
    except (json.JSONDecodeError, AttributeError, TypeError) as exc:
        raise RuntimeError("HTTP ASR bad response") from exc
    if not text:
        raise RuntimeError("empty transcript")
    return text


def _transcribe_http(
    audio: Path,
    language: str,
    model: str | None = None,
    *,
    _retried: bool = False,
) -> str:
    del model, _retried
    backends = asr_backends()
    if not backends:
        raise RuntimeError("no HTTP ASR credentials")
    last_exc: Exception | None = None
    for backend in backends:
        try:
            return _transcribe_http_once(audio, language or "fa", backend)
        except Exception as exc:
            last_exc = exc
            logger.info("HTTP ASR backend failed; trying next")
            continue
    raise last_exc or RuntimeError("HTTP ASR request failed")


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
    awaiting_confirm: bool = True,
    telegram_user_id: int | None = None,
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
        store.set_person_fact(member_key, "notes", FACT_LATEST, transcript, source="voice")
        store.set_person_fact(member_key, "notes", FACT_LATEST_SUMMARY, summary, source="voice")
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
        if awaiting_confirm:
            mark_voice_pending(
                store,
                member_key,
                telegram_message_id=telegram_message_id,
                chat_id=chat_id,
                telegram_user_id=telegram_user_id,
            )
            store.log_person_event(
                member_key,
                "voice_awaiting_confirm",
                payload={
                    "transcript": transcript,
                    "telegram_message_id": telegram_message_id,
                    "chat_id": chat_id,
                },
                telegram_message_id=telegram_message_id,
            )
    return summary, rid


def username_for_member(store: TaskStore | None, member_key: str | None) -> str | None:
    if not member_key:
        return None
    if store is not None:
        for mapping in store.list_user_mappings():
            if mapping.member_key == member_key and mapping.username:
                return mapping.username
    return KNOWN_USERNAMES.get(member_key)


def mention_for_member(store: TaskStore | None, member_key: str | None) -> str:
    uname = username_for_member(store, member_key)
    if uname:
        return "@" + uname.lstrip("@")
    if member_key:
        return member_display_fa(member_key)
    return "گوینده"


def quote_transcript_html(transcript: str) -> str:
    return f"<blockquote>{escape(transcript or '')}</blockquote>"


def confirmation_prompt(
    member_key: str | None,
    transcript: str,
    *,
    username: str | None = None,
    store: TaskStore | None = None,
) -> str:
    """Full transcript in a blockquote + one short confirm ask. Never a 280-char summary."""
    if username:
        mention = "@" + username.lstrip("@")
    else:
        mention = mention_for_member(store, member_key)
    return f"{quote_transcript_html(transcript)}\n{mention} {CONFIRM_ASK_FA}"


def reask_prompt(
    transcript: str, member_key: str | None = None, store: TaskStore | None = None
) -> str:
    mention = mention_for_member(store, member_key) if member_key else ""
    ask = f"{mention} {REASK_FA}".strip() if mention else REASK_FA
    return f"{quote_transcript_html(transcript)}\n{ask}"


def confirmed_prompt(
    member_key: str | None,
    transcript: str,
    *,
    store: TaskStore | None = None,
) -> str:
    mention = mention_for_member(store, member_key)
    return f"{quote_transcript_html(transcript)}\n{mention} {CONFIRMED_MARK_FA}"


def voice_confirm_button_rows(
    voice_tg_id: int,
    *,
    transcript: str = "",
    question: str | None = None,
) -> list[list[tuple[str, str]]]:
    """Chips for THIS voice confirm, bound to the transcript — not a global menu."""
    return question_buttons(
        question or CONFIRM_ASK_FA,
        kind="vc",
        context=transcript or "",
        target_id=int(voice_tg_id),
    )


def _fact_int(store: TaskStore, member_key: str, fact_key: str) -> int | None:
    raw = store.get_person_fact(member_key, "notes", fact_key) or ""
    if raw.lstrip("-").isdigit():
        return int(raw)
    return None


def resolve_pending_speaker_for_voice(store: TaskStore, voice_tg_id: int) -> str | None:
    target = str(voice_tg_id)
    for key in ("kawe", "hamed", "saman", "mohammadreza", "ghazal"):
        if not voice_is_pending(store, key):
            continue
        if (store.get_person_fact(key, "notes", FACT_PENDING_TG) or "") == target:
            return key
    return None


def speaker_may_confirm(
    store: TaskStore,
    owner_key: str | None,
    tapper_key: str | None,
    tapper_user_id: int | None = None,
) -> bool:
    if not owner_key:
        return False
    if tapper_key and tapper_key == owner_key:
        return True
    if tapper_user_id is None:
        return False
    saved = store.get_person_fact(owner_key, "notes", FACT_PENDING_USER) or ""
    return saved == str(tapper_user_id)


def mark_awaiting_edit(store: TaskStore, member_key: str) -> None:
    store.set_person_fact(member_key, "notes", FACT_PENDING_EDIT, "1", source="voice")


def is_awaiting_edit(store: TaskStore, member_key: str) -> bool:
    return (store.get_person_fact(member_key, "notes", FACT_PENDING_EDIT) or "") == "1"


def apply_voice_callback(
    store: TaskStore,
    *,
    owner_key: str,
    action: str,
    telegram_message_id: int | None = None,
) -> VoiceConfirmResult:
    voice_tg = _fact_int(store, owner_key, FACT_PENDING_TG)
    confirm_mid = _fact_int(store, owner_key, FACT_PENDING_MSG)
    chat_id = _fact_int(store, owner_key, FACT_PENDING_CHAT)
    if action in {"ok", "yes"}:
        locked = lock_voice_transcript(
            store, owner_key, telegram_message_id=telegram_message_id
        )
        return VoiceConfirmResult(
            action="confirm",
            transcript=locked,
            reply=LOCKED_FA,
            confirm_message_id=confirm_mid,
            voice_tg_id=voice_tg,
            member_key=owner_key,
            chat_id=chat_id,
        )
    if action == "edit":
        mark_awaiting_edit(store, owner_key)
        return VoiceConfirmResult(
            action="wait_edit",
            reply=EDIT_WAIT_FA,
            html=False,
            confirm_message_id=confirm_mid,
            voice_tg_id=voice_tg,
            member_key=owner_key,
            chat_id=chat_id,
        )
    return VoiceConfirmResult(action="ignored", html=False)


def mark_voice_pending(
    store: TaskStore,
    member_key: str,
    *,
    telegram_message_id: int | None = None,
    chat_id: int | None = None,
    confirm_message_id: int | None = None,
    telegram_user_id: int | None = None,
) -> None:
    store.set_person_fact(member_key, "notes", FACT_PENDING, "1", source="voice")
    store.set_person_fact(member_key, "notes", FACT_PENDING_EDIT, "", source="voice")
    if telegram_message_id is not None:
        store.set_person_fact(
            member_key, "notes", FACT_PENDING_TG, str(telegram_message_id), source="voice"
        )
    if chat_id is not None:
        store.set_person_fact(member_key, "notes", FACT_PENDING_CHAT, str(chat_id), source="voice")
    if confirm_message_id is not None:
        store.set_person_fact(
            member_key, "notes", FACT_PENDING_MSG, str(confirm_message_id), source="voice"
        )
    if telegram_user_id is not None:
        store.set_person_fact(
            member_key, "notes", FACT_PENDING_USER, str(telegram_user_id), source="voice"
        )


def set_pending_confirm_message_id(store: TaskStore, member_key: str, message_id: int) -> None:
    store.set_person_fact(member_key, "notes", FACT_PENDING_MSG, str(message_id), source="voice")


def clear_voice_pending(store: TaskStore, member_key: str) -> None:
    for key in (
        FACT_PENDING,
        FACT_PENDING_TG,
        FACT_PENDING_MSG,
        FACT_PENDING_CHAT,
        FACT_PENDING_EDIT,
        FACT_PENDING_USER,
    ):
        store.set_person_fact(member_key, "notes", key, "", source="voice")


def voice_is_pending(store: TaskStore, member_key: str | None) -> bool:
    if not member_key:
        return False
    return (store.get_person_fact(member_key, "notes", FACT_PENDING) or "") == "1"


def pending_transcript(store: TaskStore, member_key: str) -> str | None:
    text = store.get_person_fact(member_key, "notes", FACT_LATEST)
    return text if text else None


def speaker_has_pending(
    store: TaskStore,
    member_key: str | None,
    *,
    chat_id: int | None = None,
    reply_to_message_id: int | None = None,
) -> bool:
    """True if THIS speaker has a draft waiting. Other speakers never steal it."""
    if not member_key or not voice_is_pending(store, member_key):
        return False
    saved_chat = store.get_person_fact(member_key, "notes", FACT_PENDING_CHAT) or ""
    if chat_id is not None and saved_chat and saved_chat != str(chat_id):
        # Still honor a direct reply to the confirm / original voice.
        if reply_to_message_id is None:
            return False
        confirm_id = store.get_person_fact(member_key, "notes", FACT_PENDING_MSG) or ""
        voice_id = store.get_person_fact(member_key, "notes", FACT_PENDING_TG) or ""
        return str(reply_to_message_id) in {confirm_id, voice_id}
    return True


def strip_confirm_filler(text: str) -> str:
    t = (text or "").strip()
    t = t.replace("\u200c", " ")
    t = _CONFIRM_STRIP_RE.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def is_voice_confirmation(text: str) -> bool:
    """Yes-meaning: obvious Persian/English yes-words after filler is stripped."""
    t = strip_confirm_filler(text)
    if not t or len(t) > 80:
        return False
    if not any(tok.lower() in t for tok in _YES_TOKENS):
        return False
    leftover = t
    for tok in sorted(_YES_TOKENS, key=len, reverse=True):
        leftover = leftover.replace(tok.lower(), " ")
    leftover = leftover.replace("را گفتم", " ").replace("رو گفتم", " ")
    leftover = leftover.replace("گفتم", " ").replace("همینه", " ")
    leftover = leftover.replace("است", " ").replace("شد", " ")
    leftover = re.sub(r"\s+", " ", leftover).strip()
    if leftover in {"", "را", "رو", "که", "دیگه", "دیگر"}:
        return True
    return t in {tok.lower() for tok in _YES_TOKENS}


def split_correction_and_lock(text: str) -> tuple[str, bool]:
    raw = (text or "").strip()
    if not raw:
        return raw, False
    if not _INLINE_LOCK_RE.search(raw):
        return raw, False
    cleaned = _INLINE_LOCK_RE.sub(" ", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .،,")
    return (cleaned or raw), True


def apply_voice_correction(
    store: TaskStore,
    member_key: str,
    new_text: str,
    *,
    chat_id: int | None = None,
    telegram_message_id: int | None = None,
) -> str:
    body = (new_text or "").strip()
    summary = summarize_fa(body)
    store.set_person_fact(member_key, "notes", FACT_LATEST, body, source="voice")
    store.set_person_fact(member_key, "notes", FACT_LATEST_SUMMARY, summary, source="voice")
    voice_tg = telegram_message_id
    if voice_tg is None:
        raw_id = store.get_person_fact(member_key, "notes", FACT_PENDING_TG) or ""
        if raw_id.isdigit():
            voice_tg = int(raw_id)
    saved_chat = chat_id
    if saved_chat is None:
        raw_chat = store.get_person_fact(member_key, "notes", FACT_PENDING_CHAT) or ""
        if raw_chat.lstrip("-").isdigit():
            saved_chat = int(raw_chat)
    if voice_tg is not None and saved_chat is not None:
        store.update_message_body(
            body,
            telegram_chat_id=saved_chat,
            telegram_message_id=voice_tg,
            processed=True,
        )
    store.log_person_event(
        member_key,
        "voice_corrected",
        payload={"transcript": body, "telegram_message_id": voice_tg, "chat_id": saved_chat},
        telegram_message_id=voice_tg,
    )
    return body


def lock_voice_transcript(
    store: TaskStore,
    member_key: str,
    *,
    telegram_message_id: int | None = None,
) -> str | None:
    transcript = pending_transcript(store, member_key)
    clear_voice_pending(store, member_key)
    store.log_person_event(
        member_key,
        "voice_confirmed",
        payload={"transcript": transcript},
        telegram_message_id=telegram_message_id,
    )
    return transcript


def handle_pending_voice_text(
    store: TaskStore,
    *,
    member_key: str | None,
    text: str,
    reply_to_message_id: int | None = None,
    chat_id: int | None = None,
    telegram_message_id: int | None = None,
) -> VoiceConfirmResult:
    """Speaker confirms or edits the draft. Other speakers are ignored."""
    if not speaker_has_pending(
        store, member_key, chat_id=chat_id, reply_to_message_id=reply_to_message_id
    ):
        return VoiceConfirmResult(action="ignored", html=False)
    assert member_key is not None
    raw = (text or "").strip()
    if not raw:
        return VoiceConfirmResult(action="ignored", html=False)
    confirm_mid = _fact_int(store, member_key, FACT_PENDING_MSG)
    voice_tg = _fact_int(store, member_key, FACT_PENDING_TG)
    waiting_edit = is_awaiting_edit(store, member_key)
    extra = dict(
        confirm_message_id=confirm_mid,
        voice_tg_id=voice_tg,
        member_key=member_key,
        chat_id=chat_id,
    )
    if is_learn_utterance(raw) and not waiting_edit:
        return VoiceConfirmResult(action="ignored", html=False)
    if waiting_edit:
        store.set_person_fact(member_key, "notes", FACT_PENDING_EDIT, "", source="voice")
    elif is_voice_confirmation(raw):
        locked = lock_voice_transcript(
            store, member_key, telegram_message_id=telegram_message_id
        )
        return VoiceConfirmResult(
            action="confirm", transcript=locked, reply=LOCKED_FA, **extra
        )
    corrected, inline_lock = split_correction_and_lock(raw)
    apply_voice_correction(
        store,
        member_key,
        corrected,
        chat_id=chat_id,
        telegram_message_id=None,
    )
    if inline_lock and not waiting_edit:
        locked = lock_voice_transcript(
            store, member_key, telegram_message_id=telegram_message_id
        )
        return VoiceConfirmResult(
            action="correct_and_lock",
            transcript=locked or corrected,
            reply=LOCKED_FA,
            **extra,
        )
    return VoiceConfirmResult(
        action="correct",
        transcript=corrected,
        reply=reask_prompt(corrected, member_key, store),
        **extra,
    )


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
        stored = store.get_person_fact(member_key, "notes", FACT_LATEST)
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
    telegram_user_id: int | None = None,
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
        awaiting_confirm=True,
        telegram_user_id=telegram_user_id,
    )
    reply = confirmation_prompt(member_key, transcript, store=store)
    return VoiceResult(
        transcript=transcript,
        summary=summary,
        reply=reply,
        member_key=member_key,
        message_row_id=rid,
        awaiting_confirm=True,
        voice_message_id=message_id,
    )
