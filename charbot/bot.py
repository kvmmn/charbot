"""Telegram bot handlers and application factory."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html import escape as html_escape

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

from charbot.agent import run_colleague
from charbot.buttons import (
    choice_means_changed,
    choice_means_done,
    choice_means_wait,
    followup_question,
    parse_callback_data,
    question_buttons,
    task_pick_buttons,
)
from charbot.config import Settings
from charbot.formatting import (
    HELP_TEXT,
    format_person_list_messages,
    format_resolved,
    format_task,
    format_task_confirmation,
    format_task_list,
    format_task_question,
    owner_group_count,
)
from charbot.intent import (
    CALLBACK_ID_PERSON,
    PERSON_CALLBACK_ID,
    SpeechAct,
    SpeechActKind,
    classify_speech_act,
    is_completion_report,
    may_create_task,
    must_reply,
)
from charbot.jobs import followup as scheduled_followup
from charbot.jobs import standup as scheduled_standup
from charbot.jobs.common import JobMessage
from charbot.members import (
    MEMBER_BY_KEY,
    chase_via,
    find_member_in_text,
    followup_addressee_fa,
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
from charbot.report import (
    berlin_today,
    month_bounds,
    parse_report_request,
    render_period_report,
    week_bounds,
)
from charbot.store import Task, TaskStatus, TaskStore
from charbot.understand import clean_work_text, extract_task
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


LIST_SEND_DELAY = 0.5  # seconds between person-list messages; avoids 429s


async def _send_html_sequence(
    message,
    bot,
    chat_id: int,
    texts: list[str],
    *,
    delay: float | None = None,
) -> None:
    """Send a read-only HTML list as intro + one message per person."""
    if not texts:
        return
    pause = LIST_SEND_DELAY if delay is None else delay
    await message.reply_text(texts[0], parse_mode=ParseMode.HTML)
    for body in texts[1:]:
        if pause:
            await asyncio.sleep(pause)
        await bot.send_message(chat_id=chat_id, text=body, parse_mode=ParseMode.HTML)


MENTION_HINTS = ("منشن", "منشن کن", "صداش کن", "صدا کن", "تگ کن", "mention", "صدا بزن")
WORK_HINTS = (
    "جلسه",
    "نتیجه جلسه",
    "تحویل",
    "دوشنبه",
    "سشنبه",
    "سه‌شنبه",
    "لوگو",
    "لینک",
    "فوروارد",
    "پیگیری",
    "غزل",
    "نمونه",
    "اینستا",
    "پالت",
    "وظایف",
    "کارها",
    "تا روز",
    "تا پایان",
    "پروژه",
    "کارگاه",
    "مشتری",
)
ROLE_TITLE_HINTS = (
    "نقش من",
    "نقشم",
    "مدیرعامل",
    "نایب رئیس",
    "رئیس هیئت",
    "حسابدار",
    "سرپرست طراحی",
    "طراح",
    "کارشناس",
    "مهندس",
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


FOLLOWUP_HINTS = ("متوجه شدی", "فهمیدی", "ذخیره کن", "چی شد")
ASK_WHO_TEXT = "بگو از کی یا از چه کاری می‌پرسی."


@dataclass
class WorkFollowup:
    task: Task | None = None
    reply: str | None = None
    created: bool = False
    context_text: str | None = None
    completed: bool = False
    button_rows: list[list[tuple[str, str]]] | None = None


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


CREATE_DRAFT_KEY = "create_draft"

_MATCH_STOP = frozenset(
    {
        "من",
        "رو",
        "را",
        "و",
        "برای",
        "که",
        "الان",
        "این",
        "آن",
        "شد",
        "شده",
        "دادم",
        "دادیم",
        "خودت",
        "گفته",
        "بودی",
        "بوده",
        "امروزه",
        "امروز",
        "دارم",
        "میدم",
        "می‌دم",
        "نه",
        "تسک",
        "فعالیت",
        "جدید",
        "معرفی",
        "کنم",
        "تموم",
        "تمام",
        "انجام",
        "تکمیل",
        "تحویل",
        "فرستادم",
        "فرستادیم",
        "کارم",
        "خلاص",
        "اوکی",
        "گزارش",
        "کار",
        "بود",
        "است",
        "هست",
        "دیگه",
        "done",
        "finished",
        "delivered",
        "completed",
    }
)


def save_pending_create_draft(store: TaskStore, speaker_key: str | None, payload: str) -> None:
    if not speaker_key:
        return
    store.set_person_fact(speaker_key, "notes", CREATE_DRAFT_KEY, payload or "1", source="create")


def clear_pending_create_draft(store: TaskStore, speaker_key: str | None) -> None:
    if not speaker_key:
        return
    store.set_person_fact(speaker_key, "notes", CREATE_DRAFT_KEY, "", source="create")


def has_pending_create_draft(store: TaskStore, speaker_key: str | None) -> bool:
    if not speaker_key:
        return False
    return bool(store.get_person_fact(speaker_key, "notes", CREATE_DRAFT_KEY))


def _content_tokens(text: str) -> set[str]:
    t = (text or "").replace("\u200c", " ")
    t = re.sub(r"[؟?!.،,;؛:()\[\]\"']+", " ", t)
    parts = [p.lower() for p in t.split() if p]
    return {p for p in parts if p not in _MATCH_STOP and len(p) >= 2}


def _rank_open_matches(tasks: list[Task], text: str, *, today: date) -> list[Task]:
    """Score candidate open tasks by title/description overlap; due-today is a boost."""
    raw = text or ""
    utt = _content_tokens(raw)
    scored: list[tuple[int, Task]] = []
    for task in tasks:
        hay = f"{task.title or ''} {task.description or ''}"
        toks = _content_tokens(hay)
        overlap = len(utt & toks)
        sub = sum(1 for tok in toks if len(tok) >= 3 and tok in raw)
        score = overlap * 2 + min(sub, 3)
        if task.due_date == today:
            score += 1
        title = (task.title or "").strip()
        if title and len(title) >= 3 and (title in raw or raw in title):
            score += 3
        similar = _similar_open_task_title(title, raw)
        if similar:
            score += 2
        if score >= 2:
            scored.append((score, task))
    scored.sort(key=lambda item: (-item[0], item[1].id))
    if not scored:
        return []
    if len(scored) == 1:
        return [scored[0][1]]
    best, second = scored[0][0], scored[1][0]
    if best >= second + 2:
        return [scored[0][1]]
    return [task for _score, task in scored[:4]]


def _similar_open_task_title(title: str, text: str) -> bool:
    needle = (title or "").strip()
    hay = (text or "").strip()
    if not needle or not hay:
        return False
    if needle in hay or hay in needle:
        return True
    return False


def ack_done_fa(title: str) -> str:
    work = html_escape((title or "کار").strip() or "کار")
    return f"اوکی. {work} را تکمیل شده زدم."


def complete_reported_work(
    store: TaskStore,
    *,
    chat_id: int,
    raw: str,
    speaker_key: str | None,
    speaker_user_id: int | None = None,
    today: date | None = None,
) -> WorkFollowup:
    """Match a completion report to work the speaker may close. Never create.

    Candidates are the speaker's own open tasks, tasks chased via them
    (Hamed for Ghazal), and — when the speaker is Kawe — every open task
    in the group. Ranking still requires a clear title match.
    """
    today = today or date.today()
    clear_pending_create_draft(store, speaker_key)
    mine = open_tasks_for_completion(store, chat_id, speaker_key)
    matches = _rank_open_matches(mine, raw, today=today)
    if len(matches) == 1:
        task = store.mark_done(
            matches[0].id,
            chat_id,
            actor_key=speaker_key,
            actor_user_id=speaker_user_id,
        )
        if task:
            return WorkFollowup(
                task=task,
                reply=ack_done_fa(task.title),
                created=False,
                completed=True,
            )
    ask = "کدام کار را تمام کردی؟"
    if len(matches) >= 2:
        shown = matches[:4]
        return WorkFollowup(
            reply=ask,
            created=False,
            button_rows=task_pick_buttons([(t.title, t.id) for t in shown]),
        )
    if not mine:
        return WorkFollowup(
            reply="کار بازی روی تو نیست. اگر کار دیگری است بگو کدام.",
            created=False,
        )
    shown = mine[:4]
    return WorkFollowup(
        reply=ask,
        created=False,
        button_rows=task_pick_buttons([(t.title, t.id) for t in shown]),
    )


def open_tasks_for(store: TaskStore, group_id: int, person_key: str | None) -> list[Task]:
    tasks = store.list_open_tasks(group_id)
    if person_key:
        return [t for t in tasks if t.assignee_key == person_key]
    return tasks


def open_tasks_for_completion(
    store: TaskStore, group_id: int, speaker_key: str | None
) -> list[Task]:
    """Open tasks a speaker may mark done: own, chase-via, or (Kawe) the board.

    Assignee on the card does not change. Ghazal stays ``ghazal``; Hamed is
    her chase contact so her work is in his completion set. Kawe coordinates
    and often reports on behalf of others. Callers still rank; a clear title
    match wins, and ambiguous sets are not auto-completed.
    """
    tasks = store.list_open_tasks(group_id)
    if not speaker_key or speaker_key == "kawe":
        return tasks
    return [
        t
        for t in tasks
        if t.assignee_key == speaker_key or chase_via(t.assignee_key) == speaker_key
    ]


def render_open_task_messages(
    store: TaskStore,
    group_id: int,
    act: SpeechAct,
    speaker_key: str | None,
) -> list[str]:
    """Board-wide lists are intro + one message per person. A named-person
    answer («کارهای سامان») stays a single ``format_task_list`` message."""
    if act.board_open:
        return format_person_list_messages(store.list_open_tasks(group_id), header="کارهای باز")
    person = act.person_key or (speaker_key if act.for_speaker else None)
    if person is None:
        return format_person_list_messages(store.list_open_tasks(group_id), header="کارهای باز")
    if act.for_speaker or person == speaker_key:
        header = "کارهای تو"
    else:
        header = f"کارهای {member_display_fa(person)}"
    return [format_task_list(open_tasks_for(store, group_id, person), header=header)]


def render_open_tasks(
    store: TaskStore,
    group_id: int,
    act: SpeechAct,
    speaker_key: str | None,
) -> str:
    """Joined preview of ``render_open_task_messages``. Live senders that
    answer a board-wide list must send the sequence, not this string."""
    return "\n\n".join(render_open_task_messages(store, group_id, act, speaker_key))


def render_role(store: TaskStore, person_key: str) -> str:
    role = store.get_person_role(person_key)
    name = member_display_fa(person_key)
    if role:
        return f"{name}: {role}"
    return f"نقش {name} را هنوز ندارم. {_mention_for(store, person_key)} یک جمله بگو، ثبت می‌کنم."


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
    if act.kind == SpeechActKind.REPORT_DONE or is_completion_report(raw):
        return complete_reported_work(
            store,
            chat_id=chat_id,
            raw=raw,
            speaker_key=speaker_key,
            speaker_user_id=speaker_user_id,
            today=today,
        )
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
        if is_completion_report(raw):
            return complete_reported_work(
                store,
                chat_id=chat_id,
                raw=raw,
                speaker_key=speaker_key,
                speaker_user_id=speaker_user_id,
                today=today,
            )
        if speaker_key and (may_create_task(raw) or act.kind == SpeechActKind.CREATE_TASK):
            save_pending_create_draft(store, speaker_key, understood.title or raw)
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
            reply=format_task_confirmation(existing),
            created=False,
            context_text=context_text,
        )
    task = _safe_create_task(
        store,
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
        reply=format_task_confirmation(task),
        created=True,
        context_text=context_text,
    )


def _mention_for(store: TaskStore, key: str) -> str:
    for mapping in store.list_user_mappings():
        if mapping.member_key == key and mapping.username:
            return f"@{mapping.username}"
    return member_display(key)


def chase_mention_for(store: TaskStore, assignee_key: str) -> str:
    """@mention the chase contact for follow-up, not the assignee if they are absent."""
    via = chase_via(assignee_key) or assignee_key
    return _mention_for(store, via)


def _role_saved(store: TaskStore, key: str) -> bool:
    return bool(store.get_person_role(key))


def _missing_roles(store: TaskStore) -> list[str]:
    return store.list_people_missing_role()


def _maybe_map_speaker(store: TaskStore, user) -> str | None:
    if not user or user.is_bot:
        return None
    mapping = store.get_user_mapping(user.id)
    key = (
        mapping.member_key
        if mapping
        else (
            resolve_member_name(user.first_name or "") or resolve_member_name(user.full_name or "")
        )
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
        await bot.send_message(
            chat_id=chat_id,
            text="نقش هر چهار نفر ثبت شد. بعد می‌رویم سراغ کارهای جاری.",
        )
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
        await update.effective_message.reply_text("چاربات فقط در گروه شرکت فعال است.")
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
        "سلام، من چاربات هستم. هماهنگ‌کننده چهارستون.\n"
        "/help را بزن یا همین‌جا حرف بزن، صدا یا عکس بفرست.",
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

    task = _safe_create_task(
        store,
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
        await update.effective_message.reply_text("Usage: /due 3 tomorrow — or /due 3 2026-03-15")
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
    texts = format_person_list_messages(tasks, header="کارهای باز")
    await _send_html_sequence(update.effective_message, context.bot, group_id, texts)


async def cmd_overdue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if await _reject_if_not_allowed(update, settings):
        return
    group_id = _require_group(update)
    if group_id is None:
        return

    store: TaskStore = context.bot_data["store"]
    tasks = store.list_overdue_tasks(group_id)
    header = "کارهای عقب‌افتاده"
    if owner_group_count(tasks) > 1:
        texts = format_person_list_messages(tasks, header=header)
    else:
        texts = [format_task_list(tasks, header=header)]
    await _send_html_sequence(update.effective_message, context.bot, group_id, texts)


async def cmd_standup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Read-only daily plan («برنامهٔ امروز»): intro + one message per person.

    No keyboard on the list. If exactly one task needs a decision (overdue
    or unowned) it is followed by ONE active card so the reply stays
    answerable without flooding the group. With 0 or 2+ decisions pending,
    the plan stands alone — the periodic follow-up job (see followup_job)
    handles a larger backlog one card at a time.
    """
    settings: Settings = context.bot_data["settings"]
    if await _reject_if_not_allowed(update, settings):
        return
    group_id = _require_group(update)
    if group_id is None:
        return

    store: TaskStore = context.bot_data["store"]

    class StandupSender:
        def __init__(self):
            self.first = True

        async def send(self, message: JobMessage):
            if self.first:
                self.first = False
                return await update.effective_message.reply_text(
                    message.text, parse_mode=ParseMode.HTML
                )
            return await context.bot.send_message(
                chat_id=message.chat_id,
                text=message.text,
                parse_mode=ParseMode.HTML,
                reply_markup=_markup(message.keyboard or []),
            )

    await scheduled_standup.run(store, StandupSender(), group_id, send_delay=LIST_SEND_DELAY)


async def _reply_task_created(update: Update, task: Task) -> None:
    await update.effective_message.reply_text(
        format_task_confirmation(task),
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
        task = _safe_create_task(
            store,
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
            html = confirmed_prompt(speaker_key, pending.transcript or "", store=store)
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
        html = render_period_report(store, group_id, period.start, period.end, label=period.label)
        rows = question_buttons("گزارش کدام بازه؟", kind="rp", context="هفته ماه", target_id=0)
        await message.reply_text(html, parse_mode=ParseMode.HTML, reply_markup=_markup(rows))
        return

    act = classify_speech_act(raw, speaker_key=speaker_key)
    if act.kind == SpeechActKind.LIST_TASKS:
        texts = render_open_task_messages(store, group_id, act, speaker_key)
        await _send_html_sequence(message, context.bot, group_id, texts)
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
        rows = question_buttons("کارهاش یا نقشش؟", kind="qa", context="کارهاش نقشش", target_id=pid)
        if tasks:
            # The list is read-only (rule: lists explain). The follow-up
            # question about role lives in its own message with its own
            # keyboard, right above its own buttons.
            html = render_open_tasks(
                store,
                group_id,
                SpeechAct(SpeechActKind.LIST_TASKS, person_key=target),
                speaker_key,
            )
            await message.reply_text(html, parse_mode=ParseMode.HTML)
            await message.reply_text(
                f"نقش {member_display_fa(target)} را هم بخوای بگو.",
                reply_markup=_markup(rows),
            )
        else:
            await message.reply_text(
                f"{member_display_fa(target)} را کارهاش را می‌خواهی یا نقشش؟",
                reply_markup=_markup(rows),
            )
        return

    if act.kind in (SpeechActKind.LEARN, SpeechActKind.CHECKIN):
        result = run_colleague(store, group_id=group_id, text=raw, act=act, speaker_key=speaker_key)
        await message.reply_text(result.reply)
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
    if act.kind == SpeechActKind.REPORT_DONE or is_completion_report(raw):
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
            await message.reply_text(
                follow.reply,
                parse_mode=ParseMode.HTML,
                reply_markup=_markup(follow.button_rows or []),
            )
            return
        if must_reply(act, raw):
            result = run_colleague(
                store, group_id=group_id, text=raw, act=act, speaker_key=speaker_key
            )
            await message.reply_text(result.reply)
        return

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
            await message.reply_text(
                follow.reply,
                parse_mode=ParseMode.HTML,
                reply_markup=_markup(follow.button_rows or []),
            )
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
        role_ask = "نقش" in raw and not any(w in raw for w in ("کارها", "تسک", "وظایف"))
        if classic_question or (addressed and role_ask) or (role_ask and _is_question(raw)):
            # Role only when they asked نقش/سمت. Named person + question is not a role dump.
            target = None
            if role_ask:
                target = named
                if (
                    target is None
                    and mapping
                    and any(w in raw for w in ("نقش من", "نقشم", "نقش منو", "منو نمیدون"))
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
                result = run_colleague(
                    store, group_id=group_id, text=raw, act=act, speaker_key=speaker_key
                )
                await message.reply_text(result.reply)
            return

        greet = ("سلام", "هی", "درود", "خوبی", "ازگل")
        if addressed and any(g in raw for g in greet) and len(raw) < 40:
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
                    mapping.member_key,
                    "notes",
                    "latest_work",
                    raw[:4000],
                    source=mapping.member_key,
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
            result = run_colleague(
                store, group_id=group_id, text=raw, act=act, speaker_key=speaker_key
            )
            await message.reply_text(result.reply)
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
        task = _safe_create_task(
            store,
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
                (
                    f"Assigned #{task.id} → {member_display(parsed.assignee_key)}.\n"
                    f"{format_task(task)}"
                ),
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
            texts = format_person_list_messages(
                store.list_open_tasks(group_id), header="کارهای باز"
            )
        else:
            person = parsed.assignee_key or speaker_key
            act = SpeechAct(
                SpeechActKind.LIST_TASKS,
                person_key=person,
                for_speaker=person == speaker_key,
            )
            texts = render_open_task_messages(store, group_id, act, speaker_key)
        await _send_html_sequence(message, context.bot, group_id, texts)
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
        header = "کارهای عقب‌افتاده"
        if owner_group_count(tasks) > 1:
            texts = format_person_list_messages(tasks, header=header)
        else:
            texts = [format_task_list(tasks, header=header)]
        await _send_html_sequence(message, context.bot, group_id, texts)


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
            if datetime.now(UTC) - when < timedelta(hours=6):
                return
        except ValueError:
            pass
    try:
        await _ask_missing_roles(context.bot, store, gid)
        store.set_kv("last_role_ask", datetime.now(UTC).isoformat())
    except Exception:
        logger.exception("setup nudge failed")


# --- Follow-up presentation: digest -> one active card -> resolved edit ---
#
# A shared group must never see several actionable messages at once. Each
# run of followup_job sends ONE read-only digest for orientation, then
# either a small burst (<=3 cards, only when each targets a DIFFERENT named
# person) or a single active card followed by a persisted queue: the rest
# are delivered one at a time as each prior card is resolved (see
# _advance_followup_queue, called from handle_callback).
FOLLOWUP_MAX_CANDIDATES = 5
FOLLOWUP_BURST_LIMIT = 3
FOLLOWUP_SEND_DELAY = 0.6  # seconds between sends in one run; avoids 429s
_FOLLOWUP_QUEUE_PREFIX = "followup_queue:"


def _dedupe_tasks(tasks: list[Task]) -> list[Task]:
    seen: set[int] = set()
    out: list[Task] = []
    for t in tasks:
        if t.id in seen:
            continue
        seen.add(t.id)
        out.append(t)
    return out


def _burst_eligible(tasks: list[Task]) -> bool:
    """<=3 items, each for a different named person -> safe to send together."""
    if not (1 <= len(tasks) <= FOLLOWUP_BURST_LIMIT):
        return False
    named = [chase_via(t.assignee_key) for t in tasks if t.assignee_key]
    return len(named) == len(set(named))


def _followup_queue_key(chat_id: int) -> str:
    return f"{_FOLLOWUP_QUEUE_PREFIX}{chat_id}"


def _load_followup_queue(store: TaskStore, chat_id: int) -> list[int]:
    raw = store.get_kv(_followup_queue_key(chat_id))
    if not raw:
        return []
    try:
        ids = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(ids, list):
        return []
    return [int(i) for i in ids if str(i).lstrip("-").isdigit()]


def _save_followup_queue(store: TaskStore, chat_id: int, ids: list[int]) -> None:
    store.set_kv(_followup_queue_key(chat_id), json.dumps(list(ids)))


def _followup_ask_text(task: Task) -> str:
    title = html_escape(task.title or "")
    owner = followup_addressee_fa(task.assignee_key)
    return followup_question(title, owner)


def _followup_markup(task: Task) -> InlineKeyboardMarkup | None:
    ask = _followup_ask_text(task)
    rows = question_buttons(ask, kind="fu", context=task.title or "", target_id=task.id)
    return _markup(rows)


async def _send_active_card(bot, chat_id: int, task: Task) -> None:
    """The ONE next item needing an answer: one card, one keyboard."""
    ask = _followup_ask_text(task)
    text = format_task_question(task, ask)
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=_followup_markup(task),
    )


async def _advance_followup_queue(bot, store: TaskStore, chat_id: int) -> None:
    """After a card resolves, send the next queued card for this chat, if any."""
    ids = _load_followup_queue(store, chat_id)
    while ids:
        next_id = ids.pop(0)
        task = store.get_task(next_id, chat_id)
        if task is None or task.status not in (TaskStatus.OPEN, TaskStatus.IN_PROGRESS):
            continue
        _save_followup_queue(store, chat_id, ids)
        await _send_active_card(bot, chat_id, task)
        return
    _save_followup_queue(store, chat_id, [])


async def _run_followup_for_group(bot, store: TaskStore, group_id: int) -> None:
    class BotSender:
        async def send(self, message: JobMessage):
            return await bot.send_message(
                chat_id=message.chat_id,
                text=message.text,
                parse_mode=ParseMode.HTML,
                reply_markup=_markup(message.keyboard or []),
            )

    await scheduled_followup.run(
        store,
        BotSender(),
        group_id,
        max_candidates=FOLLOWUP_MAX_CANDIDATES,
        burst_limit=FOLLOWUP_BURST_LIMIT,
        send_delay=FOLLOWUP_SEND_DELAY,
    )


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
        try:
            await _run_followup_for_group(context.bot, store, group_id)
        except Exception:
            logger.exception("Follow-up failed for group %s", group_id)


def _tapped_label(query) -> str | None:
    """The label of the button the user actually tapped, read from the
    message's markup as it was before we edit it — the source of truth for
    "what they chose" in the resolved-edit text."""
    message = getattr(query, "message", None)
    markup = getattr(message, "reply_markup", None) if message else None
    if not markup:
        return None
    for row in markup.inline_keyboard:
        for btn in row:
            if (getattr(btn, "callback_data", None) or "") == query.data:
                return getattr(btn, "text", None)
    return None


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
        if not speaker_may_confirm(store, owner, tapper_key, user.id if user else None):
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
                await query.edit_message_text(html, parse_mode=ParseMode.HTML, reply_markup=None)
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
        if not (
            choice_means_done(choice) or choice_means_wait(choice) or choice_means_changed(choice)
        ):
            await query.answer()
            return

        # Read the tapped label from the ORIGINAL markup before we touch it —
        # that is the only accurate source for "what they chose".
        tapped_label = _tapped_label(query) or "پاسخ داده شد"
        task = store.get_task(task_id, chat_id)
        if tapper_key:
            who = member_display_fa(tapper_key)
        elif task and task.assignee_key:
            who = followup_addressee_fa(task.assignee_key) or "کسی"
        else:
            who = "کسی"

        if choice_means_done(choice):
            store.mark_done(
                task_id, chat_id, actor_key=tapper_key, actor_user_id=user.id if user else None
            )
            await query.answer("انجام شد.")
        elif choice_means_wait(choice):
            await query.answer("باشه.")
        else:
            await query.answer()

        # Edit path: same message, keyboard gone, who answered + what they
        # chose. Falls back to just stripping the keyboard if the edit is
        # rejected (e.g. message too old), building on _strip_task_buttons.
        resolved = format_resolved(who, tapped_label)
        try:
            await query.edit_message_text(resolved, parse_mode=ParseMode.HTML, reply_markup=None)
        except Exception:
            try:
                await query.edit_message_reply_markup(
                    reply_markup=_strip_task_buttons(query, task_id)
                )
            except Exception:
                pass

        if choice_means_changed(choice) and message:
            await message.reply_text("بگو چی عوض شد.")

        # Advance: send the next queued card for this chat, if a follow-up
        # batch left one waiting.
        await _advance_followup_queue(context.bot, store, chat_id)
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
        rows = question_buttons("گزارش کدام بازه؟", kind="rp", context="هفته ماه", target_id=0)
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
                rows = voice_confirm_button_rows(message_id, transcript=result.transcript)
                sent = await msg.reply_text(
                    result.reply,
                    parse_mode=ParseMode.HTML,
                    reply_markup=_markup(rows),
                )
                if result.member_key and sent is not None:
                    set_pending_confirm_message_id(store, result.member_key, sent.message_id)
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
        MessageHandler(
            filters.VOICE | filters.AUDIO | filters.PHOTO | filters.Document.ALL,
            handle_media,
        )
    )

    if app.job_queue:
        app.job_queue.run_repeating(setup_nudge_job, interval=180, first=180)
        if settings.followup_enabled:
            interval = max(settings.followup_interval_hours, 1) * 3600
            app.job_queue.run_repeating(followup_job, interval=interval, first=interval)

    return app
