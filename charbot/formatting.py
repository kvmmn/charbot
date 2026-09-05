"""Telegram presentation layer: cards, lists, digests, active questions, edits.

Governing rule: **lists explain; cards ask; edits close the loop.**

No other module should invent its own text layout or keyboard shape — bot.py
and report.py call into here (and into charbot/buttons.py for keyboards) so
every surface in the group reads the same way. See docs/UI-GUIDELINES.md for
the full design contract with rendered examples of every message type.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from charbot.members import chase_via, member_display_fa
from charbot.store import Task

BERLIN = ZoneInfo("Europe/Berlin")
TODAY_LATE_HOUR = 16  # Europe/Berlin: afternoon pressure on due-today meta

# ---------------------------------------------------------------------------
# Persian digits + the Jalali (Solar Hijri) calendar.
# Dates read in Persian by name ("۷ شهریور"), never as bare Gregorian slashes.
# ---------------------------------------------------------------------------

_ASCII_TO_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def to_fa_digits(value: int | str) -> str:
    """Render any ASCII digits in ``value`` as Persian digits."""
    return str(value).translate(_ASCII_TO_FA_DIGITS)


JALALI_MONTHS = (
    "فروردین",
    "اردیبهشت",
    "خرداد",
    "تیر",
    "مرداد",
    "شهریور",
    "مهر",
    "آبان",
    "آذر",
    "دی",
    "بهمن",
    "اسفند",
)


def _gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Standard Gregorian→Jalali conversion (public-domain algorithm)."""
    g_d_m = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        355666
        + 365 * gy
        + (gy2 + 3) // 4
        - (gy2 + 99) // 100
        + (gy2 + 399) // 400
        + gd
        + g_d_m[gm - 1]
    )
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + (days % 31)
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return jy, jm, jd


def jalali_label(d: date) -> str:
    """``2026-08-29`` → ``۷ شهریور`` — day + Persian month name, Persian digits."""
    _, jm, jd = _gregorian_to_jalali(d.year, d.month, d.day)
    return f"{to_fa_digits(jd)} {JALALI_MONTHS[jm - 1]}"


def format_time_fa(dt: datetime) -> str:
    return to_fa_digits(f"{dt.hour:02d}:{dt.minute:02d}")


# ---------------------------------------------------------------------------
# Identity: the written name is primary. The ring is only ever a secondary
# cue glued to the name — never a stand-in status code.
# ---------------------------------------------------------------------------

PERSON_MARK = {
    "kawe": "🔵",
    "hamed": "🟢",
    "saman": "🟠",
    "mohammadreza": "🟡",
    "ghazal": "🟣",
}
UNASSIGNED_MARK = "⚪"
UNASSIGNED_LABEL = "بدون مسئول"


def person_mark(key: str | None) -> str:
    if not key:
        return UNASSIGNED_MARK
    return PERSON_MARK.get(key, UNASSIGNED_MARK)


def person_label(key: str | None) -> str:
    """Ring + full name together (identity is never ring-only)."""
    if not key:
        return f"{UNASSIGNED_MARK} {UNASSIGNED_LABEL}"
    return f"{person_mark(key)} {escape(member_display_fa(key))}"


# ---------------------------------------------------------------------------
# Due-date phrasing. Natural Persian sentences, no "·" separator.
# ---------------------------------------------------------------------------


def _berlin_clock(now: datetime | None = None) -> datetime:
    clock = now or datetime.now(BERLIN)
    if clock.tzinfo is None:
        return clock.replace(tzinfo=BERLIN)
    return clock.astimezone(BERLIN)


def _due_sentence(
    due_date: date | None,
    today: date,
    *,
    now: datetime | None = None,
) -> str:
    """Full sentence for an active card / single-task focus.

    Absolute calendar bands (Europe/Berlin day), not soft synonyms:
    overdue keeps «N روز عقب‌افتاده»; today is «موعد امروز» (with
    «هنوز مانده» after ~16:00); tomorrow is «موعد فردا» — never «۰ روز».
    """
    if due_date is None:
        return "بدون موعد"
    label = jalali_label(due_date)
    if due_date < today:
        days = (today - due_date).days
        return f"موعد {label}، {to_fa_digits(days)} روز عقب‌افتاده"
    if due_date == today:
        if _berlin_clock(now).hour >= TODAY_LATE_HOUR:
            return "موعد امروز، هنوز مانده"
        return "موعد امروز"
    if due_date == today + timedelta(days=1):
        return "موعد فردا"
    days = (due_date - today).days
    return f"موعد {label}، {to_fa_digits(days)} روز مانده"


def _due_short(due_date: date | None, today: date) -> str:
    """Compact phrase (no day-count) — used inside lists and digests."""
    if due_date is None:
        return "بدون موعد"
    if due_date == today:
        return "موعد امروز"
    if due_date == today + timedelta(days=1):
        return "موعد فردا"
    return f"موعد {jalali_label(due_date)}"


# ---------------------------------------------------------------------------
# Progressive disclosure. Reserved for the actual overflow payload — never
# used as decoration around a type label or a metadata line.
# ---------------------------------------------------------------------------

LIST_INLINE_MAX = 6
DIGEST_INLINE_MAX = 6


def wrap_expandable(inner: str) -> str:
    return f"<blockquote expandable>{inner}</blockquote>"


# ---------------------------------------------------------------------------
# Single task: title (subject) + one natural metadata line. No blockquote —
# blockquote-for-everything flattens rank. This is the shared "payload" used
# both stand-alone (format_task_confirmation) and inside an active card.
# ---------------------------------------------------------------------------


def format_task(
    task: Task, *, today: date | None = None, now: datetime | None = None
) -> str:
    today = today or date.today()
    title = escape((task.title or "").strip() or "بدون عنوان")
    who = person_label(task.assignee_key)
    due = _due_sentence(task.due_date, today, now=now)
    return f"<b>{title}</b>\n{who}، {due}"


def format_task_confirmation(
    task: Task, *, type_label: str = "ثبت شد", note: str | None = None, today: date | None = None
) -> str:
    """<type label> + blank + optional note + the task payload. No keyboard."""
    lines = [f"<b>{escape(type_label)}</b>", ""]
    if note:
        lines.append(note)
    lines.append(format_task(task, today=today))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Flat read-only lists: /open, /overdue, "کارهای X". Numbered, never buttoned.
# ---------------------------------------------------------------------------


def _numbered_line(n: int, task: Task, today: date) -> str:
    title = escape((task.title or "").strip() or "بدون عنوان")
    due = _due_short(task.due_date, today)
    owner = person_label(task.assignee_key)
    return f"{to_fa_digits(n)}. {title} — {due} — {owner}"


def _flat_body(tasks: list[Task], today: date) -> str:
    lines = [_numbered_line(i + 1, t, today) for i, t in enumerate(tasks)]
    head, rest = lines[:LIST_INLINE_MAX], lines[LIST_INLINE_MAX:]
    body = "\n".join(head)
    if rest:
        body += "\n" + wrap_expandable("\n".join(rest))
    return body


def format_task_list(tasks: list[Task], *, header: str, today: date | None = None) -> str:
    """Read-only. Never attach a reply_markup to this text — see rule in
    docs/UI-GUIDELINES.md: lists explain, they never ask."""
    today = today or date.today()
    if not tasks:
        return f"<b>{escape(header)}</b>\n\nچیزی در لیست نیست."
    return f"<b>{escape(header)}</b>\n\n{_flat_body(tasks, today)}"


def owner_group_count(tasks: list[Task]) -> int:
    """Distinct assignee keys in ``tasks`` (``None`` counts as one group)."""
    return len({t.assignee_key for t in tasks})


def format_daily_plan(
    tasks: list[Task], *, decisions: int | None = None, today: date | None = None
) -> str:
    """Joined preview of the daily plan. Live send uses
    ``format_person_list_messages`` so Telegram gets intro + one message
    per person. ``decisions`` is accepted for call-site compatibility and
    is not shown — the follow-up job still hands out the active cards."""
    del decisions
    today = today or date.today()
    if not tasks:
        return "<b>برنامهٔ امروز</b>\n\nکاری در لیست نیست."
    return "\n\n".join(format_person_list_messages(tasks, header="برنامهٔ امروز", today=today))


# ---------------------------------------------------------------------------
# Person-by-person lists: intro, then one message per owner. Most overdue
# people first, unassigned last. Live senders emit each string separately
# so the eye never has to scan a stacked wall of names.
# ---------------------------------------------------------------------------


def _sort_key(task: Task, today: date) -> tuple[int, date, int]:
    if task.due_date is None:
        return (2, date.max, task.id)
    if task.due_date < today:
        return (0, task.due_date, task.id)
    return (1, task.due_date, task.id)


def _group_by_person(tasks: list[Task], today: date) -> list[tuple[str | None, list[Task]]]:
    buckets: dict[str | None, list[Task]] = {}
    for t in tasks:
        buckets.setdefault(t.assignee_key, []).append(t)
    groups = [
        (key, sorted(group, key=lambda t: _sort_key(t, today))) for key, group in buckets.items()
    ]

    def priority(item: tuple[str | None, list[Task]]) -> tuple[tuple[int, date, int], bool]:
        key, group = item
        return (_sort_key(group[0], today), key is None)

    groups.sort(key=priority)
    return groups


def _people_summary(
    groups: list[tuple[str | None, list[Task]]],
    total: int,
    unassigned_label: str,
) -> str:
    n_people = sum(1 for key, _ in groups if key)
    has_unassigned = any(key is None for key, _ in groups)
    summary = f"{to_fa_digits(total)} کار"
    if n_people:
        summary += f" برای {to_fa_digits(n_people)} نفر"
    if has_unassigned:
        summary += " و بدون مسئول" if n_people else f" {unassigned_label}"
    return summary


def _person_message(
    key: str | None,
    group_tasks: list[Task],
    today: date,
    unassigned_label: str,
) -> str:
    """One read-only message: ring+name once in the header, numbered items."""
    name = unassigned_label if key is None else escape(member_display_fa(key))
    ring = person_mark(key)
    head = f"<b>{ring} {name} — {to_fa_digits(len(group_tasks))} کار</b>"
    via = chase_via(key)
    if key and via and via != key:
        head += f"\nپیگیری از {escape(member_display_fa(via))}"
    lines = [
        f"{to_fa_digits(i + 1)}. {escape((t.title or '').strip() or 'بدون عنوان')}"
        f" — {_due_short(t.due_date, today)}"
        for i, t in enumerate(group_tasks)
    ]
    visible, overflow = lines[:LIST_INLINE_MAX], lines[LIST_INLINE_MAX:]
    body = "\n".join(visible)
    if overflow:
        body += "\n" + wrap_expandable("\n".join(overflow))
    return head + "\n" + body


def order_tasks_for_cards(tasks: list[Task], *, today: date | None = None) -> list[Task]:
    """Flatten person groups for sequential active cards.

    Within a band: most-urgent person first, finish that person's items
    before jumping to the next person. Unassigned last.
    """
    today = today or date.today()
    ordered: list[Task] = []
    for _, group in _group_by_person(tasks, today):
        ordered.extend(group)
    return ordered


def format_person_list_messages(
    tasks: list[Task],
    *,
    header: str,
    today: date | None = None,
    unassigned_label: str = UNASSIGNED_LABEL,
) -> list[str]:
    """Sequence of read-only HTML messages: intro, then one per person.

    Message 0 is the type label + summary only. Each following message is
    one owner (unassigned last). Callers must send them as separate Telegram
    messages — do not stack them back into one bubble on the live path.
    """
    today = today or date.today()
    if not tasks:
        return [f"<b>{escape(header)}</b>\n\nچیزی در لیست نیست."]
    groups = _group_by_person(tasks, today)
    intro = f"<b>{escape(header)}</b>\n\n{_people_summary(groups, len(tasks), unassigned_label)}"
    people = [
        _person_message(key, group_tasks, today, unassigned_label) for key, group_tasks in groups
    ]
    return [intro, *people]


def format_task_digest(
    tasks: list[Task],
    *,
    header: str,
    today: date | None = None,
    unassigned_label: str = UNASSIGNED_LABEL,
) -> str:
    """Joined preview of the person-by-person digest. Live send uses
    ``format_person_list_messages`` so each person is its own Telegram
    message. Ring+name appears once per person, in that message's header."""
    today = today or date.today()
    if not tasks:
        return f"<b>{escape(header)}</b>\n\nچیزی نیست."
    return "\n\n".join(
        format_person_list_messages(
            tasks, header=header, today=today, unassigned_label=unassigned_label
        )
    )


# ---------------------------------------------------------------------------
# Active card: the ONE next item that genuinely needs an answer. One
# keyboard, built by charbot/buttons.py, is attached by the caller.
# ---------------------------------------------------------------------------


def format_active_card(subject: str, meta: str) -> str:
    return f"<b>پاسخ لازم</b>\n\n{subject}\n{meta}"


def format_task_question(
    task: Task,
    question: str,
    *,
    today: date | None = None,
    now: datetime | None = None,
) -> str:
    today = today or date.today()
    meta = _due_sentence(task.due_date, today, now=now)
    if not task.assignee_key:
        meta = f"{UNASSIGNED_LABEL}، {meta}"
    return format_active_card(question, meta)


# ---------------------------------------------------------------------------
# Resolved edit: what the digest/active-card message becomes once someone
# taps. Same message, keyboard gone, who answered + what they chose.
# ---------------------------------------------------------------------------


def format_resolved(who: str, chosen_label: str, *, when: datetime | None = None) -> str:
    when = when or datetime.now()
    today = date.today()
    time_str = format_time_fa(when)
    when_word = "امروز" if when.date() == today else jalali_label(when.date())
    return f"<b>ثبت شد</b>\n\n{who}: {chosen_label}\nپاسخ {when_word}، {time_str}"


# ---------------------------------------------------------------------------
# Escalation alert for a single badly-overdue item: owner + consequence +
# suggested next action. Distinct from the plural "کارهای عقب‌افتاده" digest.
# ---------------------------------------------------------------------------


def format_overdue_alert(
    task: Task, *, consequence: str, next_action: str, today: date | None = None
) -> str:
    today = today or date.today()
    title = escape((task.title or "").strip() or "بدون عنوان")
    who = person_label(task.assignee_key)
    due = _due_sentence(task.due_date, today)
    return (
        f"<b>عقب‌افتاده</b>\n\n"
        f"{who} — {title}\n"
        f"{due}؛ {escape(consequence)}\n"
        f"پیشنهاد: {escape(next_action)}"
    )


HELP_TEXT = """<b>چاربات</b> هماهنگ‌کننده گروه.

کار یعنی عنوان، مسئول، موعد. بقیه در توضیح می‌ماند و در لیست نمی‌آید.

بگو کار چیست، برای کی، تا کی.
/open کارهای باز
/overdue عقب‌افتاده
"""


def format_followup_queue_notice(count: int) -> str:
    return f"{to_fa_digits(count)} کار دیگر در نوبت است."
