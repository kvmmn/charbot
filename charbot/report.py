"""Period follow-through in the group — facts, not an HR score."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from charbot.formatting import to_fa_digits, wrap_expandable
from charbot.members import BOARD_MEMBERS, member_display_fa
from charbot.nlp import parse_date
from charbot.store import TaskStatus, TaskStore

BERLIN = ZoneInfo("Europe/Berlin")
REPORT_PEOPLE = tuple(m.key for m in BOARD_MEMBERS)  # board then staff (Ghazal)

_MONTHS_FA = (
    "ژانویه",
    "فوریه",
    "مارس",
    "آوریل",
    "مه",
    "ژوئن",
    "ژوئیه",
    "اوت",
    "سپتامبر",
    "اکتبر",
    "نوامبر",
    "دسامبر",
)
_RANGE_RE = re.compile(
    r"گزارش\s+از\s+(.+?)\s+تا\s+(.+?)(?:\s|$)",
)
_WEEK_MARKERS = ("این هفته", "گزارش هفته", "هفته‌ای", "هفتگی")
_MONTH_MARKERS = ("این ماه", "گزارش ماه", "ماهانه", "ماهی")


@dataclass
class PersonPeriod:
    slug: str
    display: str
    done_in_period: int = 0
    still_open: int = 0
    overdue: int = 0
    on_time_done: int = 0
    late_done: int = 0


@dataclass
class PeriodSpec:
    start: date
    end: date
    label: str = ""


def berlin_today(now: datetime | None = None) -> date:
    clock = now or datetime.now(BERLIN)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=BERLIN)
    return clock.astimezone(BERLIN).date()


def berlin_date(dt: datetime | None) -> date | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BERLIN)
    return dt.astimezone(BERLIN).date()


def week_bounds(today: date) -> tuple[date, date]:
    start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=6)
    return start, end


def month_bounds(today: date) -> tuple[date, date]:
    start = today.replace(day=1)
    if start.month == 12:
        nxt = start.replace(year=start.year + 1, month=1)
    else:
        nxt = start.replace(month=start.month + 1)
    end = nxt - timedelta(days=1)
    return start, end


def parse_report_request(text: str, *, today: date | None = None) -> PeriodSpec | None:
    raw = (text or "").strip()
    if "گزارش" not in raw:
        return None
    today = today or berlin_today()
    m = _RANGE_RE.search(raw)
    if m:
        start = parse_date(m.group(1), today=today)
        end = parse_date(m.group(2), today=today)
        if start and end:
            if end < start:
                start, end = end, start
            return PeriodSpec(start=start, end=end, label="بازه")
    if any(h in raw for h in _MONTH_MARKERS) or re.search(r"گزارش\s+ماه", raw):
        start, end = month_bounds(today)
        return PeriodSpec(start=start, end=end, label="این ماه")
    if any(h in raw for h in _WEEK_MARKERS) or re.search(r"گزارش\s+هفته", raw):
        start, end = week_bounds(today)
        return PeriodSpec(start=start, end=end, label="این هفته")
    if raw.strip() in {"گزارش", "گزارش کار", "گزارش کارها"}:
        start, end = week_bounds(today)
        return PeriodSpec(start=start, end=end, label="این هفته")
    return None


def _in_period(completed: date | None, start: date, end: date) -> bool:
    return completed is not None and start <= completed <= end


def period_report(
    store: TaskStore,
    chat_id: int,
    start: date,
    end: date,
    *,
    today: date | None = None,
) -> list[PersonPeriod]:
    today = today or berlin_today()
    tasks = store.list_group_tasks(chat_id)
    by_slug = {
        key: PersonPeriod(slug=key, display=member_display_fa(key))
        for key in REPORT_PEOPLE
    }
    for task in tasks:
        slug = task.assignee_key
        if slug not in by_slug:
            continue
        row = by_slug[slug]
        if task.status in (TaskStatus.OPEN, TaskStatus.IN_PROGRESS):
            row.still_open += 1
            if task.due_date is not None and task.due_date < today:
                row.overdue += 1
        if task.status == TaskStatus.DONE:
            done_on = berlin_date(task.completed_at)
            if _in_period(done_on, start, end):
                row.done_in_period += 1
                if task.due_date is None or (done_on is not None and done_on <= task.due_date):
                    row.on_time_done += 1
                else:
                    row.late_done += 1
    return [by_slug[k] for k in REPORT_PEOPLE]


def format_day_fa(d: date) -> str:
    return f"{to_fa_digits(d.day)} {_MONTHS_FA[d.month - 1]} {to_fa_digits(d.year)}"


def format_range_fa(start: date, end: date) -> str:
    if start == end:
        return format_day_fa(start)
    if start.month == end.month and start.year == end.year:
        return f"{start.day} تا {end.day} {_MONTHS_FA[end.month - 1]} {end.year}"
    return f"{format_day_fa(start)} تا {format_day_fa(end)}"


def format_period_report(
    people: list[PersonPeriod],
    start: date,
    end: date,
    *,
    label: str | None = None,
) -> str:
    """<b>گزارش {label}</b> — totals first, per-person detail in ONE
    expandable blockquote (never one blockquote per person: that was the
    "everything is a card" flood this replaces)."""
    title = label or "بازه"
    done = sum(row.done_in_period for row in people)
    still_open = sum(row.still_open for row in people)
    overdue = sum(row.overdue for row in people)
    totals = (
        f"{to_fa_digits(done)} انجام‌شده، {to_fa_digits(still_open)} مانده، "
        f"{to_fa_digits(overdue)} عقب‌افتاده"
    )

    under: list[str] = []
    detail_lines = []
    for row in people:
        detail_lines.append(
            f"{row.display} — انجام‌شده {to_fa_digits(row.done_in_period)}، "
            f"به‌موقع {to_fa_digits(row.on_time_done)}، دیر {to_fa_digits(row.late_done)}، "
            f"باز {to_fa_digits(row.still_open)}، عقب‌افتاده {to_fa_digits(row.overdue)}"
        )
        if row.done_in_period == 0 and (row.still_open or row.overdue):
            under.append(row.display)

    parts = [
        f"<b>گزارش {title}</b>",
        "",
        f"بازه {format_range_fa(start, end)}",
        totals,
        "",
        wrap_expandable("\n".join(detail_lines)),
    ]
    if under:
        who = "، ".join(under)
        parts.append(
            f"{who} این بازه انجام‌شده‌ای ندارد و کار باز یا عقب‌افتاده دارد — کم‌کاری احتمالی."
        )
    return "\n".join(parts)


def render_period_report(
    store: TaskStore,
    chat_id: int,
    start: date,
    end: date,
    *,
    today: date | None = None,
    label: str | None = None,
) -> str:
    people = period_report(store, chat_id, start, end, today=today)
    return format_period_report(people, start, end, label=label)
