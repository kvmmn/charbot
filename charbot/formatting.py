"""Clean task cards for Telegram (blockquote, color ring per person)."""

from __future__ import annotations

from datetime import date
from html import escape

from charbot.members import member_display_fa
from charbot.store import Task

# Telegram cannot color the quote bar. A colored ring is the person mark.
PERSON_MARK = {
    "kawe": "🔵",
    "hamed": "🟢",
    "saman": "🟠",
    "mohammadreza": "🟡",
    "ghazal": "🟣",
}
UNASSIGNED_MARK = "⚪"


def person_mark(key: str | None) -> str:
    if not key:
        return UNASSIGNED_MARK
    return PERSON_MARK.get(key, UNASSIGNED_MARK)


def _due_label(d: date | None) -> str:
    if not d:
        return "بدون موعد"
    label = f"{d.day}/{d.month}"
    if d < date.today():
        label += " عقب‌افتاده"
    return label


def format_task(task: Task, *, locale_hint: str = "fa") -> str:
    """One blockquote card: title, then ring + due + owner. Matches group UI."""
    del locale_hint
    title = escape(task.title.strip() or "بدون عنوان")
    owner = escape(member_display_fa(task.assignee_key))
    due = escape(_due_label(task.due_date))
    mark = person_mark(task.assignee_key)
    return f"<blockquote><b>{title}</b>\n{mark} {due} {owner}</blockquote>"


def format_task_list(tasks: list[Task], *, header: str) -> str:
    if not tasks:
        return f"<b>{escape(header)}</b>\nچیزی در لیست نیست."
    cards = "\n".join(format_task(t) for t in tasks)
    return f"<b>{escape(header)}</b>\n{cards}"


HELP_TEXT = """<b>چاربات</b> هماهنگ‌کننده چهارستون.

کار یعنی عنوان، مسئول، موعد. بقیه در توضیح می‌ماند و در لیست نمی‌آید.

بگو کار چیست، برای کی، تا کی.
/open کارهای باز
/overdue عقب‌افتاده
"""
