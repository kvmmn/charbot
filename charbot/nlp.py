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
    LIST_OVERDUE = "list_overdue"
    NONE = "none"


@dataclass
class ParsedNL:
    intent: NLIntent
    task_id: int | None = None
    title: str | None = None
    assignee_key: str | None = None
    due_date: date | None = None


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
    re.compile(r"^(?:task|تسک|کار)\s*[:\-]?\s*(.+)$", re.IGNORECASE),
    re.compile(r"^(?:new task|تسک جدید)\s*[:\-]?\s*(.+)$", re.IGNORECASE),
    re.compile(r"^(?:we need to|باید)\s+(.+)$", re.IGNORECASE),
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
    re.compile(r"^(?:open tasks?|tasks? open|تسک(?:‌|\s)?های باز|لیست تسک)$", re.IGNORECASE),
]

_LIST_OVERDUE_PATTERNS = [
    re.compile(r"^(?:overdue|late tasks?|تسک(?:‌|\s)?های عقب(?:‌|\s)?افتاده)$", re.IGNORECASE),
]


def parse_natural_language(text: str, today: date | None = None) -> ParsedNL:
    """Parse free-form group messages for task intents."""
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

    for pat in _CREATE_PATTERNS:
        m = pat.match(normalized)
        if m:
            title = m.group(1).strip()
            if len(title) >= 3:
                return ParsedNL(intent=NLIntent.CREATE_TASK, title=title)

    return ParsedNL(intent=NLIntent.NONE)
