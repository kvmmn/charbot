"""Telegram bot handlers and application factory."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
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
from charbot.store import Task, TaskStore
from charbot.voice import (
    answer_voice_question,
    is_voice_question,
    media_dest,
    process_incoming_voice,
)

logger = logging.getLogger(__name__)

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

    task = store.create_task(
        group_id=group_id,
        title=title,
        assignee_key=assignee_key,
        created_by_user_id=user.id if user else None,
    )
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
    await update.effective_message.reply_text("\n\n".join(parts), parse_mode=ParseMode.HTML)


async def _reply_task_created(update: Update, task: Task) -> None:
    await update.effective_message.reply_text(
        "نوشته شد.\n" + format_task(task),
        parse_mode=ParseMode.HTML,
    )


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

    if len(message.text.strip()) < 4:
        return

    store: TaskStore = context.bot_data["store"]
    raw = message.text.strip()
    named = find_member_in_text(raw)
    replied = message.reply_to_message
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

    parsed = parse_natural_language(message.text)
    mentioned = bool(message.entities) and any(
        getattr(e, "type", None) in ("mention", "text_mention") for e in message.entities
    )

    if parsed.intent == NLIntent.NONE:
        user = update.effective_user
        _maybe_map_speaker(store, user)
        mapping = store.get_user_mapping(user.id) if user else None
        addressed = _bot_addressed(message, raw)
        wants_mention = any(h in raw for h in MENTION_HINTS)

        # Collective role/work ask — answer instantly, no @mention required.
        if is_board_overview(raw):
            await message.reply_text(format_board_overview(store))
            return

        # Questions — always answer, never dump a role sermon.
        # Clear work/role questions in this allowed group do not need an @mention.
        classic_question = any(h in raw for h in QUESTION_HINTS)
        role_ask = "نقش" in raw or "چیکار" in raw or "چی کار" in raw
        if classic_question or (addressed and role_ask) or (role_ask and _is_question(raw)):
            target = named
            if target is None and mapping and any(
                w in raw for w in ("نقش من", "نقشم", "نقش منو", "منو نمیدون")
            ):
                target = mapping.member_key
            if target:
                role = store.get_person_role(target)
                if role:
                    await message.reply_text(f"{member_display(target)}: {role}")
                else:
                    await message.reply_text(
                        f"نقش {member_display(target)} را هنوز ندارم. "
                        f"{_mention_for(store, target)} یک جمله بگو، ثبت می‌کنم."
                    )
            elif "نقش" in raw:
                await message.reply_text(format_board_overview(store))
            elif addressed:
                await message.reply_text("بگو از کی یا از چه کاری می‌پرسی.")
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
        return

    if parsed.intent == NLIntent.CREATE_TASK and parsed.title:
        user = update.effective_user
        assignee = None
        if user:
            mapping = store.get_user_mapping(user.id)
            if mapping:
                assignee = mapping.member_key
        task = store.create_task(
            group_id=group_id,
            title=parsed.title,
            assignee_key=assignee,
            created_by_user_id=user.id if user else None,
        )
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

    if parsed.intent == NLIntent.LIST_OPEN:
        tasks = store.list_open_tasks(group_id)
        await message.reply_text(
            format_task_list(tasks, header="کارهای باز"), parse_mode=ParseMode.HTML
        )
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

        parts = ["<b>پیگیری روزانه</b>"]
        if overdue:
            parts.append(format_task_list(overdue[:5], header="عقب‌افتاده"))
        if unowned:
            parts.append(format_task_list(unowned[:5], header="بدون مسئول"))
        try:
            await context.bot.send_message(
                chat_id=group_id, text="\n".join(parts), parse_mode=ParseMode.HTML
            )
        except Exception:
            logger.exception("Follow-up failed for group %s", group_id)


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
        try:
            result = await process_incoming_voice(
                store=store,
                bot=context.bot,
                chat_id=msg.chat_id,
                message_id=msg.message_id,
                file_id=getattr(media, "file_id", None),
                kind=kind,
                member_key=member_key,
                existing_path=str(dest) if dest.is_file() else None,
            )
            await msg.reply_text(result.reply)
        except Exception:
            logger.exception("voice pipeline failed")
            await msg.reply_text("صدا را گرفتم ولی نتوانستم پیاده‌اش کنم.")
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
