"""Telegram bot handlers and application factory."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from charbot.buttons import (
    choice_means_changed,
    choice_means_done,
    choice_means_wait,
    followup_question,
    parse_callback_data,
    question_buttons,
)
from charbot.config import Settings
from charbot.formatting import HELP_TEXT, format_task, format_task_list
from charbot.members import (
    MEMBER_BY_KEY,
    find_member_in_text,
    member_display,
    member_display_fa,
    resolve_member_name,
)
from charbot.nlp import (
    NLIntent,
    parse_assign_command,
    parse_done_command,
    parse_due_command,
    parse_natural_language,
    parse_task_command,
)
from charbot.understand import clean_work_text, extract_task
from charbot.glossary import (
    ack_learn,
    apply_to_open_tasks,
    extract_glossary_entries,
    upsert_entries,
)
from charbot.intent import (
    CALLBACK_ID_PERSON,
    PERSON_CALLBACK_ID,
    SpeechAct,
    SpeechActKind,
    classify_speech_act,
    may_create_task,
    must_reply,
)
from charbot.report import (
    berlin_today,
    month_bounds,
    parse_report_request,
    render_period_report,
    week_bounds,
)
from charbot.store import Task, TaskStore
from charbot.voice import (
    ASR_FAIL_FA,
    EDIT_WAIT_FA,
    LOCKED_FA,
    WRONG_SPEAKER_FA,
    answer_voice_question,
    apply_voice_callback,
    confirmed_prompt,
    handle_pending_voice_text,
    is_voice_question,
    media_dest,
    process_incoming_voice,
    resolve_pending_speaker_for_voice,
    set_pending_confirm_message_id,
    speaker_may_confirm,
    voice_confirm_button_rows,
)

logger = logging.getLogger(__name__)


def _safe_create_task(store: TaskStore, **kwargs):
    """Last-line persistence guard. Question-shaped titles never insert."""
    try:
        return store.create_task(**kwargs)
    except ValueError:
        logger.info("refused question-shaped task title")
        return None



def _markup(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup | None:
    if not rows:
        return None
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
            for row in rows
            if row
        ]
    )

MENTION_HINTS = ("منشن", "منشن کن", "صداش کن", "صدا کن", "تگ کن", "mention", "صدا بزن")
WORK_HINTS = (
    "جلسه", "نتیجه جلسه", "تحویل", "دوشنبه", "سشنبه", "سه‌شنبه", "لوگو",
    "لینک", "فوروارد", "پیگیری", "غزل", "نمونه", "اینستا", "پالت", "وظایف",
    "کارها", "تا روز", "تا پایان", "پروژه", "کارگاه", "مشتری",
)
ROLE_TITLE_HINTS = (
    "نقش من", "نقشم", "مدیرعامل", "نایب رئیس", "رئیس هیئت", "حسابدار",
    "سرپرست طراحی", "طراح", "کارشناس", "مهندس",
)
QUESTION_HINTS = ("؟", "?", "چیه", "چیست", "چی بود", "نمیدونی", "کی هست", "کیه")
IMPERATIVE_HINTS = ("توضیح بده", "شرح بده", "کامل بگو", "بگو")
BOARD_OVERVIEW_KEYS = ("kawe", "hamed", "saman", "mohammadreza")
BOARD_OVERVIEW_MARKERS = (
    "چهار نفر",
    "تک‌تک",
    "تک تک",
    "هیئت",
    "همه",
    "هرکدوم",
    "هر کدوم",
    "هرکدام",
    "هر کدام",
)
_SELF_ROLE_HINTS = ("نقش من", "نقشم", "نقش منو")
_STRONG_OVERVIEW_MARKERS = (
    "چهار نفر",
    "تک‌تک",
    "تک تک",
    "هرکدوم",
    "هر کدوم",
    "هرکدام",
    "هر کدام",
)


def _looks_like_work(text: str) -> bool:
    hits = sum(1 for h in WORK_HINTS if h in text)
    return hits >= 2 or ("نتیجه جلسه" in text) or text.count("\n") >= 4


def _is_question(text: str) -> bool:
    return any(h in text for h in QUESTION_HINTS) or any(h in text for h in IMPERATIVE_HINTS)


def _apply_learn(store: TaskStore, group_id: int, raw: str) -> str:
    entries = extract_glossary_entries(raw)
    if entries:
        upsert_entries(store, entries)
        apply_to_open_tasks(store, group_id, entries)
    return ack_learn(entries)


def is_board_overview(text: str) -> bool:
    """True when the speaker asks for every board member's role/work, not one person."""
    if "نقش" not in text:
        return False
    if not any(m in text for m in BOARD_OVERVIEW_MARKERS):
        return False
    self_role = any(w in text for w in _SELF_ROLE_HINTS)
    if self_role and not any(m in text for m in _STRONG_OVERVIEW_MARKERS):
        return False
    return True


def _work_note_for(store: TaskStore, key: str) -> str | None:
    preferred: str | None = None
    fallback: str | None = None
    for kind in ("notes", "note"):
        for fact in store.list_person_facts(key, kind=kind):
            fact_key = fact.get("key") or fact.get("fact_key") or ""
            val = (fact.get("value") or "").strip()
            if not val:
                continue
            if fact_key == "work_details":
                preferred = val
            elif fact_key == "latest_work" and fallback is None:
                fallback = val
    text_note = preferred or fallback
    if not text_note:
        return None
    if len(text_note) > 400:
        return text_note[:397] + "…"
    return text_note


def format_board_overview(store: TaskStore) -> str:
    """Persian snapshot of the four board members: mention, role titles, work notes."""
    lines = ["نقش و کار چهار نفر در چهارستون:"]
    for key in BOARD_OVERVIEW_KEYS:
        mention = _mention_for(store, key)
        name = member_display_fa(key)
        label = f"{name} {mention}" if mention.startswith("@") else name
        role = store.get_person_role(key)
        if role:
            lines.append(f"{label} — {role}")
        else:
            lines.append(f"{label} — نقش ثبت نشده.")
        note = _work_note_for(store, key)
        if note:
            lines.append(f"کار: {note}")
    return "\n".join(lines)


def _bot_addressed(message, text: str) -> bool:
    low = text.lower()
    if "thecharbot" in low or "چاربات" in text or "charbot" in low:
        return True
    return False


FOLLOWUP_HINTS = ("متوجه شدی", "فهمیدی", "ذخیره کن", "چی شد")
ASK_WHO_TEXT = "بگو از کی یا از چه کاری می‌پرسی."


@dataclass
class WorkFollowup:
    task: Task | None = None
    reply: str | None = None
    created: bool = False
    context_text: str | None = None


def _should_load_context(raw: str, *, force: bool = False) -> bool:
    """True for «متوجه شدی / فهمیدی / ذخیره کن / چی شد» (never for role sermons)."""
    if is_board_overview(raw):
        return False
    if any(w in raw for w in _SELF_ROLE_HINTS) or "نقش" in raw:
        return False
    if force:
        return True
    return any(h in raw for h in FOLLOWUP_HINTS)


def _is_followup_only(text: str) -> bool:
    """Skip short confirm/save prompts when walking back to the work dump."""
    if not (text or "").strip():
        return True
    if is_board_overview(text):
        return False
    if parse_natural_language(text).intent == NLIntent.CREATE_TASK:
        return False
    if _looks_like_work(text):
        return False
    if "نقش" in text:
        return False
    return any(h in text for h in FOLLOWUP_HINTS)


def load_prior_context(
    store: TaskStore,
    *,
    chat_id: int,
    raw: str,
    reply_to_text: str | None = None,
    current_message_id: int | None = None,
    force: bool = False,
) -> tuple[str | None, str | None]:
    """Previous non-bot human text in this chat, or reply_to_message text."""
    if not _should_load_context(raw, force=force):
        return None, None
    if reply_to_text and reply_to_text.strip() and not _is_followup_only(reply_to_text):
        return reply_to_text.strip(), None
    for msg in store.list_recent_human_messages(
        chat_id,
        exclude_telegram_message_id=current_message_id,
        limit=20,
    ):
        body = (msg.get("body") or "").strip()
        if not body or body == raw.strip():
            continue
        if _is_followup_only(body):
            continue
        return body, msg.get("member_key")
    return None, None


def _similar_open_task(
    store: TaskStore, group_id: int, title: str, assignee_key: str | None = None
) -> Task | None:
    needle = (title or "").strip()
    if not needle:
        return None
    for task in store.list_open_tasks(group_id):
        other = (task.title or "").strip()
        if other == needle:
            return task
        if needle in other or other in needle:
            if assignee_key is None or task.assignee_key in (None, assignee_key):
                return task
    return None



def open_tasks_for(store: TaskStore, group_id: int, person_key: str | None) -> list[Task]:
    tasks = store.list_open_tasks(group_id)
    if person_key:
        return [t for t in tasks if t.assignee_key == person_key]
    return tasks


def render_open_tasks(
    store: TaskStore,
    group_id: int,
    act: SpeechAct,
    speaker_key: str | None,
) -> str:
    if act.board_open:
        return format_task_list(store.list_open_tasks(group_id), header="کارهای باز")
    person = act.person_key or (speaker_key if act.for_speaker else None)
    if act.for_speaker or person == speaker_key:
        header = "کارهای تو"
    elif person:
        header = f"کارهای {member_display_fa(person)}"
    else:
        header = "کارهای باز"
    return format_task_list(open_tasks_for(store, group_id, person), header=header)


def render_role(store: TaskStore, person_key: str) -> str:
    role = store.get_person_role(person_key)
    name = member_display_fa(person_key)
    if role:
        return f"{name}: {role}"
    return (
        f"نقش {name} را هنوز ندارم. "
        f"{_mention_for(store, person_key)} یک جمله بگو، ثبت می‌کنم."
    )


def interpret_work_or_followup(
    store: TaskStore,
    *,
    chat_id: int,
    raw: str,
    speaker_key: str | None,
    speaker_user_id: int | None = None,
    reply_to_text: str | None = None,
    current_message_id: int | None = None,
    addressed: bool = False,
    force_context: bool = False,
    today: date | None = None,
) -> WorkFollowup:
    """Create/reuse a task from a self-obligation or from prior work context."""
    del addressed
    today = today or date.today()
    act = classify_speech_act(raw, speaker_key=speaker_key)
    if act.kind == SpeechActKind.LIST_TASKS:
        return WorkFollowup(reply=render_open_tasks(store, chat_id, act, speaker_key))
    if act.kind in (SpeechActKind.QUERY_ROLE, SpeechActKind.ASK_WHICH):
        return WorkFollowup()
    context_text, context_key = load_prior_context(
        store,
        chat_id=chat_id,
        raw=raw,
        reply_to_text=reply_to_text,
        current_message_id=current_message_id,
        force=force_context,
    )
    understood = extract_task(
        raw,
        speaker_key=speaker_key,
        today=today,
        context=context_text if context_text and _is_followup_only(raw) else None,
    )
    from_current = bool(understood.title) and not _is_followup_only(raw)
    if not understood.title and context_text:
        understood = extract_task(
            context_text,
            speaker_key=context_key or speaker_key,
            today=today,
        )
        from_current = False

    if understood.confidence == "low" and understood.ask:
        return WorkFollowup(reply=understood.ask, context_text=context_text)

    title = understood.title
    description = understood.description
    due_date = understood.due_date
    assignee = understood.assignee_key or (
        speaker_key if from_current else (context_key or speaker_key)
    )
    if not title:
        parsed = parse_natural_language(
            raw if from_current or not context_text else context_text,
            today=today,
            speaker_key=assignee,
        )
        if parsed.intent != NLIntent.CREATE_TASK or not parsed.title:
            return WorkFollowup(context_text=context_text)
        title = clean_work_text(parsed.title) or parsed.title
        description = parsed.description
        due_date = parsed.due_date
        assignee = parsed.assignee_key or assignee

    payload = raw if from_current else (context_text or raw)
    if not may_create_task(payload):
        if understood.ask:
            return WorkFollowup(reply=understood.ask, context_text=context_text)
        return WorkFollowup(context_text=context_text)

    existing = _similar_open_task(store, chat_id, title, assignee)
    if existing:
        return WorkFollowup(
            task=existing,
            reply="نوشته شد.\n" + format_task(existing),
            created=False,
            context_text=context_text,
        )
    task = _safe_create_task(store, 
        group_id=chat_id,
        title=title,
        description=description,
        assignee_key=assignee,
        due_date=due_date,
        created_by_user_id=speaker_user_id,
    )
    if task is None:
        return WorkFollowup(context_text=context_text)
    return WorkFollowup(
        task=task,
        reply="نوشته شد.\n" + format_task(task),
        created=True,
        context_text=context_text,
    )


def _mention_for(store: TaskStore, key: str) -> str:
    for mapping in store.list_user_mappings():
        if mapping.member_key == key and mapping.username:
            return f"@{mapping.username}"
    return member_display(key)


def _role_saved(store: TaskStore, key: str) -> bool:
    return bool(store.get_person_role(key))


def _missing_roles(store: TaskStore) -> list[str]:
    return store.list_people_missing_role()


def _maybe_map_speaker(store: TaskStore, user) -> str | None:
    if not user or user.is_bot:
        return None
    mapping = store.get_user_mapping(user.id)
    key = mapping.member_key if mapping else (
        resolve_member_name(user.first_name or "") or resolve_member_name(user.full_name or "")
    )
    if not key:
        return None
    store.upsert_user_mapping(
        telegram_user_id=user.id,
        member_key=key,
        username=user.username,
        display_name=user.full_name,
    )
    store.upsert_person_identity(
        key,
        user.id,
        user.username,
        user.full_name,
    )
    store.log_person_event(
        key,
        "mapped",
        payload={"telegram_user_id": user.id, "username": user.username},
    )
    return key


async def _ask_missing_roles(bot, store: TaskStore, chat_id: int) -> None:
    missing = [k for k in _missing_roles(store) if not _role_saved(store, k)]
    if not missing:
        await bot.send_message(chat_id=chat_id, text="نقش هر چهار نفر ثبت شد. بعد می‌رویم سراغ کارهای جاری.")
        return
    parts = []
    for key in missing:
        parts.append(f"{_mention_for(store, key)} نقش تو در چهارستون چیست؟ یک جمله.")
    await bot.send_message(chat_id=chat_id, text="\n".join(parts) + "\nمنتظر یکی نمی‌مانم.")


async def ingest_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Persist every text/voice/photo update for coordinator review."""
    store: TaskStore = context.bot_data["store"]
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat:
        return

    kind = "text"
    file_id = None
    media_path = None
    text = msg.text or msg.caption
    if msg.voice:
        kind = "voice"
        file_id = msg.voice.file_id
    elif msg.audio:
        kind = "audio"
        file_id = msg.audio.file_id
    elif msg.photo:
        kind = "photo"
        file_id = msg.photo[-1].file_id
    elif msg.document:
        kind = "document"
        file_id = msg.document.file_id
    elif msg.video:
        kind = "video"
        file_id = msg.video.file_id

    if file_id:
        try:
            dest = media_dest(store, chat.id, msg.message_id, kind)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.is_file():
                tg_file = await context.bot.get_file(file_id)
                await tg_file.download_to_drive(custom_path=str(dest))
            media_path = str(dest)
        except Exception:
            logger.exception("media download failed")

    store.log_inbox(
        telegram_update_id=update.update_id,
        chat_id=chat.id,
        chat_type=chat.type,
        chat_title=chat.title or chat.full_name,
        user_id=user.id if user else None,
        username=user.username if user else None,
        display_name=user.full_name if user else None,
        message_id=msg.message_id,
        kind=kind,
        text=text,
        file_id=file_id,
        media_path=media_path,
    )
    member_key = _maybe_map_speaker(store, user)
    store.log_conversation(
        chat_id=chat.id,
        direction="in",
        kind=kind,
        text=text,
        user_id=user.id if user else None,
        username=user.username if user else None,
        display_name=user.full_name if user else None,
        media_path=media_path,
        telegram_message_id=msg.message_id,
        member_key=member_key,
    )
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        store.set_kv("telegram_group_id", str(chat.id))
        if chat.title:
            store.set_kv("telegram_group_title", chat.title)


def _chat_allowed(update: Update, settings: Settings) -> bool:
    allowed = settings.allowed_groups()
    if not allowed:
        return True
    chat = update.effective_chat
    if not chat:
        return False
    return chat.id in allowed


async def _reject_if_not_allowed(update: Update, settings: Settings) -> bool:
    if _chat_allowed(update, settings):
        return False
    if update.effective_message:
        await update.effective_message.reply_text(
            "چاربات فقط در گروه شرکت فعال است."
        )
    return True


def _require_group(update: Update) -> int | None:
    chat = update.effective_chat
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return None
    return chat.id


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if await _reject_if_not_allowed(update, settings):
        return
    await update.effective_message.reply_text(
        "سلام، من چاربات هستم. هماهنگ‌کننده چهارستون.\n/help را بزن یا همین‌جا حرف بزن، صدا یا عکس بفرست.",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if await _reject_if_not_allowed(update, settings):
        return
    await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if await _reject_if_not_allowed(update, settings):
        return
    store: TaskStore = context.bot_data["store"]
    user = update.effective_user
    if not user:
        return
    mapping = store.get_user_mapping(user.id)
    name = user.full_name or user.username or str(user.id)
    if mapping:
        await update.effective_message.reply_text(
            f"You are mapped as {member_display(mapping.member_key)} "
            f"(Telegram: {name}, id={user.id})."
        )
    else:
        await update.effective_message.reply_text(
            f"Telegram: {name}, id={user.id}.\n"
            "Not mapped yet. Use /map Kawe (or Hamed, Saman, Mohammadreza)."
        )


async def cmd_map(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if await _reject_if_not_allowed(update, settings):
        return
    store: TaskStore = context.bot_data["store"]
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    args = context.args or []
    target_user_id = user.id
    target_username = user.username
    target_name = user.full_name

    if message.reply_to_message and message.reply_to_message.from_user:
        replied = message.reply_to_message.from_user
        target_user_id = replied.id
        target_username = replied.username
        target_name = replied.full_name
        if args and args[0].startswith("@"):
            args = args[1:]

    member_text = " ".join(args).strip()
    member_key = resolve_member_name(member_text)
    if not member_key:
        await message.reply_text(
            "Usage: /map Kawe — or reply to someone: /map Hamed\n"
            f"Board: {', '.join(m.display_name for m in MEMBER_BY_KEY.values())}"
        )
        return

    store.upsert_user_mapping(
        telegram_user_id=target_user_id,
        member_key=member_key,
        username=target_username,
        display_name=target_name,
    )
    store.upsert_person_identity(
        member_key,
        target_user_id,
        target_username,
        target_name,
    )
    store.log_person_event(
        member_key,
        "mapped",
        payload={"telegram_user_id": target_user_id, "username": target_username},
    )
    await message.reply_text(
        f"Mapped {target_name or target_user_id} → {member_display(member_key)}."
    )


async def cmd_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if await _reject_if_not_allowed(update, settings):
        return
    group_id = _require_group(update)
    if group_id is None:
        await update.effective_message.reply_text("Use /task inside the X-Chaharsotoon group.")
        return

    store: TaskStore = context.bot_data["store"]
    title = parse_task_command(context.args or [])
    if not title:
        await update.effective_message.reply_text("Usage: /task Ship Q3 report to client")
        return

    user = update.effective_user
    assignee_key = None
    if user:
        mapping = store.get_user_mapping(user.id)
        if mapping:
            assignee_key = mapping.member_key

    task = _safe_create_task(store, 
        group_id=group_id,
        title=title,
        assignee_key=assignee_key,
        created_by_user_id=user.id if user else None,
    )
    if task is None:
        await update.effective_message.reply_text("این سؤال است، کار نیست.")
        return
    await _reply_task_created(update, task)


async def cmd_assign(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if await _reject_if_not_allowed(update, settings):
        return
    group_id = _require_group(update)
    if group_id is None:
        return

    store: TaskStore = context.bot_data["store"]
    task_id, assignee = parse_assign_command(context.args or [])
    if task_id is None or not assignee:
        await update.effective_message.reply_text("Usage: /assign 3 Kawe")
        return

    task = store.assign_task(task_id, group_id, assignee)
    if not task:
        await update.effective_message.reply_text(f"Task #{task_id} not found.")
        return
    await update.effective_message.reply_text(
        f"Assigned #{task.id} to {member_display_fa(assignee)}.\n{format_task(task)}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_due(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if await _reject_if_not_allowed(update, settings):
        return
    group_id = _require_group(update)
    if group_id is None:
        return

    store: TaskStore = context.bot_data["store"]
    task_id, due = parse_due_command(context.args or [])
    if task_id is None or not due:
        await update.effective_message.reply_text(
            "Usage: /due 3 tomorrow — or /due 3 2026-03-15"
        )
        return

    task = store.set_due_date(task_id, group_id, due)
    if not task:
        await update.effective_message.reply_text(f"Task #{task_id} not found.")
        return
    await update.effective_message.reply_text(
        f"Due date set for #{task.id} → {due.isoformat()}.\n{format_task(task)}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if await _reject_if_not_allowed(update, settings):
        return
    group_id = _require_group(update)
    if group_id is None:
        return

    store: TaskStore = context.bot_data["store"]
    task_id = parse_done_command(context.args or [])
    if task_id is None:
        await update.effective_message.reply_text("Usage: /done 3")
        return

    task = store.mark_done(task_id, group_id)
    if not task:
        await update.effective_message.reply_text(f"Task #{task_id} not found.")
        return
    await update.effective_message.reply_text(f"Done #{task.id}: {task.title} ✓")


async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if await _reject_if_not_allowed(update, settings):
        return
    group_id = _require_group(update)
    if group_id is None:
        return

    store: TaskStore = context.bot_data["store"]
    tasks = store.list_open_tasks(group_id)
    text = format_task_list(tasks, header="کارهای باز")
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_overdue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if await _reject_if_not_allowed(update, settings):
        return
    group_id = _require_group(update)
    if group_id is None:
        return

    store: TaskStore = context.bot_data["store"]
    tasks = store.list_overdue_tasks(group_id)
    text = format_task_list(tasks, header="عقب‌افتاده")
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_standup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if await _reject_if_not_allowed(update, settings):
        return
    group_id = _require_group(update)
    if group_id is None:
        return

    store: TaskStore = context.bot_data["store"]
    open_tasks = store.list_open_tasks(group_id)
    overdue = store.list_overdue_tasks(group_id)
    unowned = store.list_unowned_open_tasks(group_id)

    parts = [format_task_list(open_tasks, header="کارهای باز")]
    if overdue:
        parts.append(format_task_list(overdue, header="عقب‌افتاده"))
    if unowned:
        parts.append(format_task_list(unowned, header="بدون مسئول"))
    watch = list(overdue) + [t for t in open_tasks if t not in overdue]
    await update.effective_message.reply_text(
        "\n\n".join(parts),
        parse_mode=ParseMode.HTML,
        reply_markup=_task_followup_markup(watch[:6]),
    )


async def _reply_task_created(update: Update, task: Task) -> None:
    await update.effective_message.reply_text(
        "نوشته شد.\n" + format_task(task),
        parse_mode=ParseMode.HTML,
    )


def confirmed_task_html(
    store: TaskStore,
    *,
    chat_id: int,
    speaker_key: str | None,
    speaker_user_id: int | None,
    transcript: str,
) -> str:
    """Task card or one clarifying ask from a locked transcript. Empty if none."""
    if classify_speech_act(transcript).kind != SpeechActKind.CREATE_TASK:
        understood = extract_task(transcript, speaker_key=speaker_key)
        return (understood.ask or "") if understood.title else ""
    understood = extract_task(transcript, speaker_key=speaker_key)
    title = understood.title
    if not title:
        return ""
    if understood.confidence == "high":
        existing = _similar_open_task(store, chat_id, title, understood.assignee_key)
        if existing:
            return format_task(existing)
        task = _safe_create_task(store, 
            group_id=chat_id,
            title=title,
            description=understood.description,
            assignee_key=understood.assignee_key,
            due_date=understood.due_date,
            created_by_user_id=speaker_user_id,
        )
        if task is None:
            return ""
        return format_task(task)
    return understood.ask or ""


def compose_voice_lock_reply(
    store: TaskStore,
    *,
    chat_id: int,
    speaker_key: str | None,
    speaker_user_id: int | None,
    transcript: str,
) -> str:
    """After the speaker locks the voice text: optional one task, no guessing."""
    extra = confirmed_task_html(
        store,
        chat_id=chat_id,
        speaker_key=speaker_key,
        speaker_user_id=speaker_user_id,
        transcript=transcript,
    )
    if extra:
        return LOCKED_FA + "\n" + extra
    return LOCKED_FA


def _voice_followup(message) -> bool:
    replied = getattr(message, "reply_to_message", None)
    if not replied:
        return False
    if not (getattr(replied, "voice", None) or getattr(replied, "audio", None)):
        return False
    text = (message.text or "").strip()
    return _is_question(text) or is_voice_question(text)


async def handle_natural_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if await _reject_if_not_allowed(update, settings):
        return
    message = update.effective_message
    if not message or not message.text:
        return
    if message.text.startswith("/"):
        return

    group_id = _require_group(update)
    if group_id is None:
        return

    store: TaskStore = context.bot_data["store"]
    raw = message.text.strip()
    user = update.effective_user
    speaker_key = _maybe_map_speaker(store, user)
    replied = message.reply_to_message
    pending = handle_pending_voice_text(
        store,
        member_key=speaker_key,
        text=raw,
        reply_to_message_id=replied.message_id if replied else None,
        chat_id=group_id,
        telegram_message_id=message.message_id,
    )
    if pending.action != "ignored":
        if pending.action in ("confirm", "correct_and_lock"):
            extra = confirmed_task_html(
                store,
                chat_id=group_id,
                speaker_key=speaker_key,
                speaker_user_id=user.id if user else None,
                transcript=pending.transcript or "",
            )
            html = confirmed_prompt(
                speaker_key, pending.transcript or "", store=store
            )
            if extra:
                html = html + "\n" + extra
            if pending.confirm_message_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=group_id,
                        message_id=pending.confirm_message_id,
                        text=html,
                        parse_mode=ParseMode.HTML,
                        reply_markup=None,
                    )
                except Exception:
                    await message.reply_text(html, parse_mode=ParseMode.HTML)
            else:
                await message.reply_text(html, parse_mode=ParseMode.HTML)
        elif pending.action == "correct":
            rows = voice_confirm_button_rows(
                pending.voice_tg_id or 0,
                transcript=pending.transcript or "",
            )
            await message.reply_text(
                pending.reply,
                parse_mode=ParseMode.HTML,
                reply_markup=_markup(rows),
            )
        else:
            await message.reply_text(
                pending.reply, parse_mode=ParseMode.HTML if pending.html else None
            )
        return

    if len(raw) < 4:
        return

    period = parse_report_request(raw)
    if period is not None:
        html = render_period_report(
            store, group_id, period.start, period.end, label=period.label
        )
        rows = question_buttons(
            "گزارش کدام بازه؟", kind="rp", context="هفته ماه", target_id=0
        )
        await message.reply_text(
            html, parse_mode=ParseMode.HTML, reply_markup=_markup(rows)
        )
        return

    act = classify_speech_act(raw, speaker_key=speaker_key)
    if act.kind == SpeechActKind.LIST_TASKS:
        await message.reply_text(
            render_open_tasks(store, group_id, act, speaker_key),
            parse_mode=ParseMode.HTML,
        )
        return
    if act.kind == SpeechActKind.QUERY_ROLE:
        if is_board_overview(raw):
            await message.reply_text(format_board_overview(store))
            return
        target = act.person_key or (speaker_key if act.for_speaker else None)
        if target:
            await message.reply_text(render_role(store, target))
        else:
            await message.reply_text(format_board_overview(store))
        return
    if act.kind == SpeechActKind.ASK_WHICH:
        target = act.person_key or speaker_key
        if not target:
            await message.reply_text("کارها را می‌خواهی یا نقش را؟ بگو مال کی.")
            return
        tasks = open_tasks_for(store, group_id, target)
        pid = PERSON_CALLBACK_ID.get(target, 0)
        rows = question_buttons(
            "کارهاش یا نقشش؟", kind="qa", context="کارهاش نقشش", target_id=pid
        )
        if tasks:
            html = render_open_tasks(
                store,
                group_id,
                SpeechAct(SpeechActKind.LIST_TASKS, person_key=target),
                speaker_key,
            )
            html += "\nاگر نقش می‌خوای، همان دکمه."
            await message.reply_text(
                html, parse_mode=ParseMode.HTML, reply_markup=_markup(rows)
            )
        else:
            await message.reply_text(
                f"{member_display_fa(target)} را کارهاش را می‌خواهی یا نقشش؟",
                reply_markup=_markup(rows),
            )
        return

    if act.kind == SpeechActKind.LEARN:
        await message.reply_text(_apply_learn(store, group_id, raw))
        return
    if act.kind == SpeechActKind.CHECKIN:
        await message.reply_text("آره.")
        return

    named = find_member_in_text(raw)
    if is_voice_question(raw) or _voice_followup(message):
        tg_mid = replied.message_id if replied and (replied.voice or replied.audio) else None
        await message.reply_text(
            answer_voice_question(
                store,
                chat_id=group_id,
                member_key=named,
                telegram_message_id=tg_mid,
            )
        )
        return

    addressed = _bot_addressed(message, raw)
    mentioned = bool(message.entities) and any(
        getattr(e, "type", None) in ("mention", "text_mention") for e in message.entities
    )
    reply_to_text = None
    if replied and not (getattr(replied, "voice", None) or getattr(replied, "audio", None)):
        reply_to_text = (replied.text or replied.caption or "").strip() or None

    user = update.effective_user
    if speaker_key is None:
        speaker_key = _maybe_map_speaker(store, user)
    if act.kind in (SpeechActKind.CREATE_TASK, SpeechActKind.CONFIRM, SpeechActKind.UNKNOWN):
        follow = interpret_work_or_followup(
            store,
            chat_id=group_id,
            raw=raw,
            speaker_key=speaker_key,
            speaker_user_id=user.id if user else None,
            reply_to_text=reply_to_text,
            current_message_id=message.message_id,
            addressed=addressed,
        )
        if follow.reply:
            await message.reply_text(follow.reply, parse_mode=ParseMode.HTML)
            return
    else:
        follow = WorkFollowup()

    parsed = parse_natural_language(message.text, speaker_key=speaker_key)
    if parsed.intent == NLIntent.NONE:
        mapping = store.get_user_mapping(user.id) if user else None
        wants_mention = any(h in raw for h in MENTION_HINTS)

        # Collective role/work ask — answer instantly, no @mention required.
        if is_board_overview(raw):
            await message.reply_text(format_board_overview(store))
            return

        # Questions — always answer, never dump a role sermon.
        # Clear work/role questions in this allowed group do not need an @mention.
        classic_question = any(h in raw for h in QUESTION_HINTS)
        role_ask = "نقش" in raw and not any(
            w in raw for w in ("کارها", "تسک", "وظایف")
        )
        if classic_question or (addressed and role_ask) or (role_ask and _is_question(raw)):
            # Role only when they asked نقش/سمت. Named person + question is not a role dump.
            target = None
            if role_ask:
                target = named
                if target is None and mapping and any(
                    w in raw for w in ("نقش من", "نقشم", "نقش منو", "منو نمیدون")
                ):
                    target = mapping.member_key
            if target and role_ask:
                await message.reply_text(render_role(store, target))
                return
            elif role_ask and "نقش" in raw:
                await message.reply_text(format_board_overview(store))
                return
            elif addressed:
                follow = interpret_work_or_followup(
                    store,
                    chat_id=group_id,
                    raw=raw,
                    speaker_key=speaker_key,
                    speaker_user_id=user.id if user else None,
                    reply_to_text=reply_to_text,
                    current_message_id=message.message_id,
                    addressed=True,
                    force_context=True,
                )
                if follow.reply:
                    await message.reply_text(follow.reply, parse_mode=ParseMode.HTML)
                    return
                if follow.context_text and (
                    _looks_like_work(follow.context_text)
                    or parse_natural_language(follow.context_text).intent == NLIntent.CREATE_TASK
                ):
                    await message.reply_text("گرفتم. اگر کار مشخصی ازش دربیاد جدا می‌نویسم.")
                    return
                u = extract_task(
                    raw,
                    speaker_key=speaker_key,
                    context=follow.context_text,
                )
                await message.reply_text(u.ask or "مسئول کیست و موعد کی است؟")
            elif must_reply(act, raw):
                await message.reply_text("آره، گوش می‌دهم.")
            return

        if addressed and any(g in raw for g in ("سلام", "هی", "درود", "خوبی", "ازگل")) and len(raw) < 40:
            await message.reply_text("جان، بگو.")
            return

        already = mapping and _role_saved(store, mapping.member_key)
        looks_like_role = (
            mapping
            and not already
            and 4 <= len(raw) <= 400
            and any(w in raw for w in ROLE_TITLE_HINTS)
            and not _looks_like_work(raw)
            and not _is_question(raw)
            and not is_board_overview(raw)
        )
        if looks_like_role and mapping:
            store.set_person_role(mapping.member_key, raw, source=mapping.member_key)
            store.log_person_event(
                mapping.member_key,
                "role_set",
                payload={"value": raw},
                telegram_message_id=message.message_id,
            )
            await message.reply_text(f"گرفتم {member_display(mapping.member_key)}، نوشتم.")
            return

        if _looks_like_work(raw) or (addressed and len(raw) > 80):
            store.set_kv("dialog", "work")
            if mapping:
                store.set_person_fact(
                    mapping.member_key, "notes", "latest_work", raw[:4000], source=mapping.member_key
                )
                store.log_person_event(
                    mapping.member_key,
                    "work_note",
                    payload={"preview": raw[:400]},
                    telegram_message_id=message.message_id,
                )
            if addressed or mentioned:
                await message.reply_text("گرفتم. اگر کار مشخصی ازش دربیاد جدا می‌نویسم.")
            return

        if wants_mention:
            missing = [k for k in _missing_roles(store) if not _role_saved(store, k)]
            if missing:
                who = " ".join(_mention_for(store, k) for k in missing)
                await message.reply_text(f"{who} نقش‌تان را یک جمله بگویید.")
            return

        if addressed:
            await message.reply_text("گوش می‌دهم. کار است، سؤال است، یا نقش؟")
            return
        if must_reply(act, raw):
            await message.reply_text("آره، گوش می‌دهم.")
            return
        return

    if parsed.intent == NLIntent.CREATE_TASK and parsed.title and may_create_task(raw):
        user = update.effective_user
        u = extract_task(message.text, speaker_key=speaker_key)
        if u.confidence == "low" and u.ask:
            await message.reply_text(u.ask)
            return
        title = clean_work_text(u.title or parsed.title) or parsed.title
        assignee = u.assignee_key
        if assignee is None and user:
            mapping = store.get_user_mapping(user.id)
            if mapping:
                assignee = mapping.member_key
        task = _safe_create_task(store, 
            group_id=group_id,
            title=title,
            description=u.description or parsed.description,
            assignee_key=assignee,
            due_date=u.due_date or parsed.due_date,
            created_by_user_id=user.id if user else None,
        )
        if task is None:
            return
        await _reply_task_created(update, task)
        return

    if parsed.intent == NLIntent.ASSIGN and parsed.task_id and parsed.assignee_key:
        task = store.assign_task(parsed.task_id, group_id, parsed.assignee_key)
        if task:
            await message.reply_text(
                f"Assigned #{task.id} → {member_display(parsed.assignee_key)}.\n{format_task(task)}",
                parse_mode=ParseMode.HTML,
            )
        return

    if parsed.intent == NLIntent.SET_DUE and parsed.task_id and parsed.due_date:
        task = store.set_due_date(parsed.task_id, group_id, parsed.due_date)
        if task:
            await message.reply_text(
                f"#{task.id} due {parsed.due_date.isoformat()}.\n{format_task(task)}",
                parse_mode=ParseMode.HTML,
            )
        return

    if parsed.intent == NLIntent.MARK_DONE and parsed.task_id:
        task = store.mark_done(parsed.task_id, group_id)
        if task:
            await message.reply_text(f"Done #{task.id} ✓")
        return

    if parsed.intent in (NLIntent.LIST_OPEN, NLIntent.LIST_TASKS, NLIntent.LIST_MINE):
        if parsed.intent == NLIntent.LIST_OPEN:
            html = format_task_list(store.list_open_tasks(group_id), header="کارهای باز")
        else:
            person = parsed.assignee_key or speaker_key
            act = SpeechAct(
                SpeechActKind.LIST_TASKS,
                person_key=person,
                for_speaker=person == speaker_key,
            )
            html = render_open_tasks(store, group_id, act, speaker_key)
        await message.reply_text(html, parse_mode=ParseMode.HTML)
        return

    if parsed.intent == NLIntent.QUERY_ROLE:
        target = parsed.assignee_key or speaker_key
        if target:
            await message.reply_text(render_role(store, target))
        else:
            await message.reply_text(format_board_overview(store))
        return

    if parsed.intent == NLIntent.LIST_OVERDUE:
        tasks = store.list_overdue_tasks(group_id)
        await message.reply_text(
            format_task_list(tasks, header="عقب‌افتاده"), parse_mode=ParseMode.HTML
        )


async def setup_nudge_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    store: TaskStore = context.bot_data["store"]
    raw_id = store.get_kv("telegram_group_id")
    if not raw_id:
        return
    if store.get_kv("dialog") == "work":
        return
    gid = int(raw_id)
    if store.list_open_tasks(gid):
        return
    missing = _missing_roles(store)
    if not missing:
        return
    last = store.get_kv("last_role_ask")
    if last:
        try:
            when = datetime.fromisoformat(last)
            if datetime.now(timezone.utc) - when < timedelta(hours=6):
                return
        except ValueError:
            pass
    try:
        await _ask_missing_roles(context.bot, store, gid)
        store.set_kv("last_role_ask", datetime.now(timezone.utc).isoformat())
    except Exception:
        logger.exception("setup nudge failed")


async def followup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gentle periodic follow-up for overdue/unowned tasks."""
    settings: Settings = context.bot_data["settings"]
    store: TaskStore = context.bot_data["store"]
    if not settings.followup_enabled:
        return

    allowed = settings.allowed_groups()
    if not allowed:
        return

    for group_id in allowed:
        overdue = store.list_overdue_tasks(group_id)
        unowned = store.list_unowned_open_tasks(group_id)
        if not overdue and not unowned:
            continue

        watch = []
        seen: set[int] = set()
        for t in list(overdue) + list(unowned):
            if t.id in seen:
                continue
            seen.add(t.id)
            watch.append(t)
        lines = ["یه نگاه به کارهای مانده:"]
        if overdue:
            lines.append(format_task_list(overdue[:5], header="عقب‌افتاده"))
        if unowned:
            lines.append(format_task_list(unowned[:5], header="بدون مسئول"))
        try:
            await context.bot.send_message(
                chat_id=group_id,
                text="\n".join(lines),
                parse_mode=ParseMode.HTML,
                reply_markup=_task_followup_markup(watch[:6]),
            )
        except Exception:
            logger.exception("Follow-up failed for group %s", group_id)


def _task_followup_markup(tasks: list[Task]) -> InlineKeyboardMarkup | None:
    rows: list[list[tuple[str, str]]] = []
    seen: set[int] = set()
    for task in tasks:
        if task.id in seen:
            continue
        seen.add(task.id)
        ask = followup_question(task.title, member_display_fa(task.assignee_key))
        rows.extend(
            question_buttons(
                ask, kind="td", context=task.title or "", target_id=task.id
            )
        )
    return _markup(rows)


def _strip_task_buttons(query, task_id: int) -> InlineKeyboardMarkup | None:
    message = getattr(query, "message", None)
    markup = getattr(message, "reply_markup", None) if message else None
    if not markup:
        return None
    needle = f":{task_id}"
    kept_rows = []
    for row in markup.inline_keyboard:
        kept = [btn for btn in row if needle not in (btn.callback_data or "")]
        if kept:
            kept_rows.append(kept)
    return InlineKeyboardMarkup(kept_rows) if kept_rows else None


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    settings: Settings = context.bot_data["settings"]
    if await _reject_if_not_allowed(update, settings):
        try:
            await query.answer()
        except Exception:
            pass
        return
    parsed = parse_callback_data(query.data)
    if not parsed:
        await query.answer()
        return
    kind, choice, payload = parsed
    store: TaskStore = context.bot_data["store"]
    user = update.effective_user
    tapper_key = _maybe_map_speaker(store, user)
    message = query.message
    chat_id = message.chat_id if message else None

    if kind == "vc":
        owner = resolve_pending_speaker_for_voice(store, int(payload))
        if not owner:
            await query.answer("دیگر لازم نیست.")
            return
        if not speaker_may_confirm(
            store, owner, tapper_key, user.id if user else None
        ):
            await query.answer(WRONG_SPEAKER_FA, show_alert=True)
            return
        result = apply_voice_callback(
            store,
            owner_key=owner,
            action=choice,
            telegram_message_id=message.message_id if message else None,
        )
        if result.action == "confirm":
            await query.answer()
            extra = confirmed_task_html(
                store,
                chat_id=chat_id or 0,
                speaker_key=owner,
                speaker_user_id=user.id if user else None,
                transcript=result.transcript or "",
            )
            html = confirmed_prompt(owner, result.transcript or "", store=store)
            if extra:
                html = html + "\n" + extra
            try:
                await query.edit_message_text(
                    html, parse_mode=ParseMode.HTML, reply_markup=None
                )
            except Exception:
                if message:
                    await message.reply_text(html, parse_mode=ParseMode.HTML)
            return
        if result.action == "wait_edit":
            await query.answer()
            if message:
                await message.reply_text(EDIT_WAIT_FA)
            return
        await query.answer()
        return

    if kind in {"td", "fu"}:
        if chat_id is None:
            await query.answer()
            return
        task_id = int(payload)
        if choice_means_done(choice):
            store.mark_done(
                task_id,
                chat_id,
                actor_key=tapper_key,
                actor_user_id=user.id if user else None,
            )
            await query.answer("انجام شد.")
            try:
                await query.edit_message_reply_markup(
                    reply_markup=_strip_task_buttons(query, task_id)
                )
            except Exception:
                pass
            return
        if choice_means_wait(choice):
            await query.answer("باشه، می‌ماند.")
            return
        if choice_means_changed(choice):
            await query.answer()
            if message:
                await message.reply_text("بگو چی عوض شد.")
            return
        await query.answer()
        return

    if kind == "qa":
        await query.answer()
        if chat_id is None:
            return
        person = CALLBACK_ID_PERSON.get(int(payload))
        if not person:
            return
        if choice == "tasks":
            html = render_open_tasks(
                store,
                chat_id,
                SpeechAct(SpeechActKind.LIST_TASKS, person_key=person),
                tapper_key,
            )
            try:
                await query.edit_message_text(html, parse_mode=ParseMode.HTML)
            except Exception:
                if message:
                    await message.reply_text(html, parse_mode=ParseMode.HTML)
            return
        if choice == "role":
            text = render_role(store, person)
            try:
                await query.edit_message_text(text)
            except Exception:
                if message:
                    await message.reply_text(text)
            return
        return

    if kind == "rp":
        await query.answer()
        if chat_id is None:
            return
        today = berlin_today()
        if choice == "week":
            start, end = week_bounds(today)
            label = "این هفته"
        elif choice == "month":
            start, end = month_bounds(today)
            label = "این ماه"
        else:
            return
        html = render_period_report(store, chat_id, start, end, today=today, label=label)
        rows = question_buttons(
            "گزارش کدام بازه؟", kind="rp", context="هفته ماه", target_id=0
        )
        try:
            await query.edit_message_text(
                html,
                parse_mode=ParseMode.HTML,
                reply_markup=_markup(rows),
            )
        except Exception:
            if message:
                await message.reply_text(
                    html, parse_mode=ParseMode.HTML, reply_markup=_markup(rows)
                )
        return

    await query.answer()


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if await _reject_if_not_allowed(update, settings):
        return
    msg = update.effective_message
    if not msg:
        return
    store: TaskStore = context.bot_data["store"]
    if msg.voice or msg.audio:
        kind = "voice" if msg.voice else "audio"
        media = msg.voice or msg.audio
        user = update.effective_user
        member_key = _maybe_map_speaker(store, user)
        dest = media_dest(store, msg.chat_id, msg.message_id, kind)
        file_id = getattr(media, "file_id", None)
        existing = str(dest) if dest.is_file() else None
        chat_id = msg.chat_id
        message_id = msg.message_id
        bot = context.bot

        async def _finish_voice() -> None:
            try:
                result = await process_incoming_voice(
                    store=store,
                    bot=bot,
                    chat_id=chat_id,
                    message_id=message_id,
                    file_id=file_id,
                    kind=kind,
                    member_key=member_key,
                    existing_path=existing,
                    telegram_user_id=user.id if user else None,
                )
                rows = voice_confirm_button_rows(
                    message_id, transcript=result.transcript
                )
                sent = await msg.reply_text(
                    result.reply,
                    parse_mode=ParseMode.HTML,
                    reply_markup=_markup(rows),
                )
                if result.member_key and sent is not None:
                    set_pending_confirm_message_id(
                        store, result.member_key, sent.message_id
                    )
            except Exception as exc:
                logger.exception("voice pipeline failed")
                text = str(exc).strip()
                try:
                    await msg.reply_text(text if "نتونستم" in text else ASR_FAIL_FA)
                except Exception:
                    logger.exception("voice fail reply failed")

        asyncio.create_task(_finish_voice())
        return
    elif msg.photo:
        await msg.reply_text("عکس را گرفتم. می‌خوانمش و اگر کار یا تصمیم باشد ثبت می‌کنم.")
    elif msg.document:
        await msg.reply_text("فایل را گرفتم.")


def build_application(settings: Settings, store: TaskStore) -> Application:
    app = Application.builder().token(settings.require_token()).build()
    app.bot_data["settings"] = settings
    app.bot_data["store"] = store

    app.add_handler(MessageHandler(filters.ALL, ingest_update), group=-1)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("task", cmd_task))
    app.add_handler(CommandHandler("assign", cmd_assign))
    app.add_handler(CommandHandler("due", cmd_due))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("open", cmd_open))
    app.add_handler(CommandHandler("overdue", cmd_overdue))
    app.add_handler(CommandHandler("standup", cmd_standup))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("map", cmd_map))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_natural_language))
    app.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO | filters.PHOTO | filters.Document.ALL, handle_media)
    )

    if app.job_queue:
        app.job_queue.run_repeating(setup_nudge_job, interval=180, first=180)
        if settings.followup_enabled:
            interval = max(settings.followup_interval_hours, 1) * 3600
            app.job_queue.run_repeating(followup_job, interval=interval, first=interval)

    return app
