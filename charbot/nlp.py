"""Rule-based natural language parsing for task-related group messages."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum

from charbot.members import resolve_member_name

# Persian digits → ASCII
_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


class NLIntent(str, Enum):
    CREATE_TASK = "create_task"
    ASSIGN = "assign"
    SET_DUE = "set_due"
    MARK_DONE = "mark_done"
    LIST_OPEN = "list_open"
    LIST_MINE = "list_mine"
    LIST_TASKS = "list_tasks"
    QUERY_ROLE = "query_role"
    LIST_OVERDUE = "list_overdue"
    NONE = "none"


@dataclass
class ParsedNL:
    intent: NLIntent
    task_id: int | None = None
    title: str | None = None
    assignee_key: str | None = None
    due_date: date | None = None
    description: str | None = None


def _normalize(text: str) -> str:
    t = text.strip().translate(_PERSIAN_DIGITS)
    t = re.sub(r"\s+", " ", t)
    return t


def parse_date(text: str, today: date | None = None) -> date | None:
    """Parse common English/Persian relative and absolute dates."""
    today = today or date.today()
    t = _normalize(text).lower()

    if t in ("today", "امروز"):
        return today
    if t in ("tomorrow", "فردا"):
        return today + timedelta(days=1)
    if t in ("next week", "هفته بعد", "هفتهٔ بعد"):
        return today + timedelta(days=7)

    # ISO yyyy-mm-dd
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    # dd/mm or dd.mm (Iranian style often dd/mm)
    m = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?", t)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            return None

    return None


def parse_command_args(text: str) -> tuple[str, list[str]]:
    """Split command text into command name and args."""
    parts = _normalize(text).split()
    if not parts:
        return "", []
    cmd = parts[0].lstrip("/").lower()
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]
    return cmd, parts[1:]


def parse_task_command(args: list[str]) -> str | None:
    if not args:
        return None
    return " ".join(args).strip()


def parse_assign_command(args: list[str]) -> tuple[int | None, str | None]:
    if len(args) < 2:
        return None, None
    try:
        task_id = int(args[0])
    except ValueError:
        return None, None
    assignee = resolve_member_name(" ".join(args[1:]))
    return task_id, assignee


def parse_due_command(args: list[str]) -> tuple[int | None, date | None]:
    if len(args) < 2:
        return None, None
    try:
        task_id = int(args[0])
    except ValueError:
        return None, None
    due = parse_date(" ".join(args[1:]))
    return task_id, due


def parse_done_command(args: list[str]) -> int | None:
    if not args:
        return None
    try:
        return int(args[0])
    except ValueError:
        return None


_CREATE_PATTERNS = [
    # Separator required. Bare «کار»/«کارهای» must never strip into a junk title.
    re.compile(r"^(?:task|new task|تسک(?:\s+جدید)?)\s*[:\-]\s*(.+)$", re.IGNORECASE),
    re.compile(r"^کار\s*[:\-]\s*(.+)$"),
    re.compile(r"^(?:we need to)\s+(.+)$", re.IGNORECASE),
]

_ASSIGN_PATTERNS = [
    re.compile(
        r"^(?:assign|اساین|واگذار)\s+(?:task\s+)?#?(\d+)\s+(?:to\s+)?(.+)$",
        re.IGNORECASE,
    ),
    re.compile(r"^#?(\d+)\s+(?:assign|اساین)\s+(?:to\s+)?(.+)$", re.IGNORECASE),
]

_DUE_PATTERNS = [
    re.compile(
        r"^(?:due|deadline|موعد|ددلاین)\s+(?:task\s+)?#?(\d+)\s+(.+)$",
        re.IGNORECASE,
    ),
    re.compile(r"^#?(\d+)\s+(?:due|deadline|موعد)\s+(.+)$", re.IGNORECASE),
]

_DONE_PATTERNS = [
    re.compile(r"^(?:done|complete|finished|انجام شد|تمام)\s+#?(\d+)\s*$", re.IGNORECASE),
    re.compile(r"^#?(\d+)\s+(?:done|complete|finished|انجام شد)\s*$", re.IGNORECASE),
]

_LIST_OPEN_PATTERNS = [
    re.compile(
        r"^(?:open tasks?|tasks? open|تسک(?:‌|\s)?های باز|لیست تسک|کارهای باز|لیست کارها)$",
        re.IGNORECASE,
    ),
]

_LIST_OVERDUE_PATTERNS = [
    re.compile(r"^(?:overdue|late tasks?|تسک(?:‌|\s)?های عقب(?:‌|\s)?افتاده)$", re.IGNORECASE),
]


def parse_natural_language(
    text: str,
    today: date | None = None,
    speaker_key: str | None = None,
) -> ParsedNL:
    """Parse free-form group messages for task intents."""
    from charbot.understand import clean_work_text, extract_task

    raw = text.strip()
    if not raw or raw.startswith("/"):
        return ParsedNL(intent=NLIntent.NONE)

    normalized = _normalize(raw)

    for pat in _DONE_PATTERNS:
        m = pat.match(normalized)
        if m:
            return ParsedNL(intent=NLIntent.MARK_DONE, task_id=int(m.group(1)))

    for pat in _ASSIGN_PATTERNS:
        m = pat.match(normalized)
        if m:
            assignee = resolve_member_name(m.group(2))
            if assignee:
                return ParsedNL(
                    intent=NLIntent.ASSIGN,
                    task_id=int(m.group(1)),
                    assignee_key=assignee,
                )

    for pat in _DUE_PATTERNS:
        m = pat.match(normalized)
        if m:
            due = parse_date(m.group(2), today=today)
            if due:
                return ParsedNL(intent=NLIntent.SET_DUE, task_id=int(m.group(1)), due_date=due)

    for pat in _LIST_OVERDUE_PATTERNS:
        if pat.match(normalized):
            return ParsedNL(intent=NLIntent.LIST_OVERDUE)

    for pat in _LIST_OPEN_PATTERNS:
        if pat.match(normalized):
            return ParsedNL(intent=NLIntent.LIST_OPEN)

    from charbot.intent import SpeechActKind, classify_speech_act, may_create_task

    act = classify_speech_act(raw, speaker_key=speaker_key)
    if act.kind == SpeechActKind.LIST_TASKS:
        if act.board_open:
            return ParsedNL(intent=NLIntent.LIST_OPEN)
        person = act.person_key or (speaker_key if act.for_speaker else None)
        return ParsedNL(intent=NLIntent.LIST_TASKS, assignee_key=person)
    if act.kind == SpeechActKind.QUERY_ROLE:
        person = act.person_key or (speaker_key if act.for_speaker else None)
        return ParsedNL(intent=NLIntent.QUERY_ROLE, assignee_key=person)
    if act.kind in (
        SpeechActKind.ASK_WHICH,
        SpeechActKind.CONFIRM,
        SpeechActKind.REPORT,
        SpeechActKind.UNKNOWN,
    ) and not may_create_task(raw):
        return ParsedNL(intent=NLIntent.NONE)

    understood = extract_task(raw, speaker_key=speaker_key, today=today)
    if understood.title and may_create_task(raw):
        return ParsedNL(
            intent=NLIntent.CREATE_TASK,
            title=understood.title,
            assignee_key=understood.assignee_key,
            due_date=understood.due_date,
            description=understood.description,
        )

    obligation = extract_self_obligation(raw, today=today)
    if obligation is not None and may_create_task(raw):
        return obligation

    if may_create_task(raw):
        for pat in _CREATE_PATTERNS:
            m = pat.match(normalized)
            if m:
                title = clean_work_text(m.group(1).strip())
                if len(title) >= 3:
                    return ParsedNL(intent=NLIntent.CREATE_TASK, title=title)

    return ParsedNL(intent=NLIntent.NONE)


_FA_NUM = {
    "یک": 1,
    "دو": 2,
    "سه": 3,
    "چهار": 4,
    "پنج": 5,
    "شش": 6,
    "هفت": 7,
    "هشت": 8,
    "نه": 9,
    "ده": 10,
}

_DUE_IN_TEXT = re.compile(
    r"تا\s+(\d+|یک|دو|سه|چهار|پنج|شش|هفت|هشت|نه|ده)\s+روز(?:\s*(?:دیگه|دیکه|دیگر))?"
)
_MENTION_RE = re.compile(r"@\S+")


def _strip_mentions(text: str) -> str:
    t = _MENTION_RE.sub(" ", text)
    t = t.replace("چاربات", " ")
    return _normalize(t)


def parse_due_in_text(text: str, today: date | None = None) -> date | None:
    """Parse relative due phrases like «تا دو روز دیگه» inside a sentence."""
    today = today or date.today()
    t = _strip_mentions(text)
    m = _DUE_IN_TEXT.search(t)
    if m:
        raw_n = m.group(1)
        n = int(raw_n) if raw_n.isdigit() else _FA_NUM.get(raw_n)
        if n is not None:
            return today + timedelta(days=n)
    if re.search(r"تا\s+فردا", t):
        return today + timedelta(days=1)
    if re.search(r"تا\s+امروز", t):
        return today
    return None


def _title_from_obligation(main: str) -> str:
    main = main.strip(" .،,")
    m = re.search(r"^(.+?)\s+رو\s+(\S+?)(?:\s+کنم|\s+کنیم|\s+کنه)?$", main)
    if m:
        obj = re.sub(r"متعلق به\s+", "", m.group(1).strip())
        verb = m.group(2).strip()
        title = f"{verb} {obj}".strip()
        title = re.sub(r"\s+", " ", title)
        return title
    title = re.sub(r"\s+کنم$|\s+کنیم$|\s+کنه$", "", main).strip()
    return title


def extract_self_obligation(text: str, today: date | None = None) -> ParsedNL | None:
    """«من باید … تا دو روز دیگه» → one CREATE_TASK; extra «اگر…» stays in description."""
    from charbot.understand import extract_task

    today = today or date.today()
    t = _strip_mentions(text)
    if "باید" not in t:
        return None
    understood = extract_task(text, today=today)
    if understood.title:
        return ParsedNL(
            intent=NLIntent.CREATE_TASK,
            title=understood.title,
            assignee_key=understood.assignee_key,
            due_date=understood.due_date,
            description=understood.description,
        )
    first_person = bool(re.search(r"من\s+باید", t) or re.search(r"باید\s+.+\s+کن(?:م|یم)\b", t))
    if not first_person:
        return None

    due = parse_due_in_text(t, today=today)
    t = _DUE_IN_TEXT.sub(" ", t)
    t = re.sub(r"تا\s+فردا", " ", t)
    t = re.sub(r"تا\s+امروز", " ", t)
    t = _normalize(t)

    description = None
    dm = re.search(r"(?:و\s+)?(اگر\s+.+)$", t)
    if dm:
        description = dm.group(1).strip()
        t = t[: dm.start()].strip()
        description = description.replace("بگم", "بگو").replace("بگیم", "بگو")
        description = description.strip(" .،,")
        if description and not description.endswith("."):
            description += "."

    mm = re.search(r"(?:من\s+)?باید\s+(.+)", t)
    if not mm:
        return None
    main = mm.group(1).strip(" .،,")
    if len(main) < 3:
        return None
    if not any(v in main for v in ("کنم", "کنیم", "کنه", "رو ")) and due is None:
        return None

    title = _title_from_obligation(main)
    if len(title) < 3:
        return None
    return ParsedNL(
        intent=NLIntent.CREATE_TASK,
        title=title,
        due_date=due,
        description=description,
    )
