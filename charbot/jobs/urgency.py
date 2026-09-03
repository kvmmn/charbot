"""Urgency follow-up: overdue / due-today / due-tomorrow as separate surfaces.

Exactly three absolute calendar bands (Europe/Berlin day via ``today``):

1. عقب‌افتاده — due_date < today
2. موعد امروز — due_date == today
3. موعد فردا — due_date == tomorrow

Bands are never merged. Active cards only for overdue + due_today (tomorrow
is heads-up list only). At most one active card in flight; the rest share
the follow-up queue with ``charbot.jobs.followup``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import date, datetime, timedelta

from charbot.formatting import (
    format_followup_queue_notice,
    format_person_list_messages,
    order_tasks_for_cards,
)
from charbot.store import Task, TaskStore

from .common import JobMessage, Sender, deliver
from .followup import (
    MAX_CANDIDATES,
    SEND_DELAY,
    active_message,
    choose_cards,
    save_queue,
)

MODE_OVERDUE = "overdue"
MODE_DUE_TODAY = "due_today"
MODE_DUE_TOMORROW = "due_tomorrow"
ALL_MODES = (MODE_OVERDUE, MODE_DUE_TODAY, MODE_DUE_TOMORROW)
CARD_MODES = (MODE_OVERDUE, MODE_DUE_TODAY)

BUCKET_HEADERS = {
    MODE_OVERDUE: "عقب‌افتاده",
    MODE_DUE_TODAY: "موعد امروز",
    MODE_DUE_TOMORROW: "موعد فردا",
}

# Urgency keeps one card in flight; escalate by resolve → next.
MAX_IN_FLIGHT = 1


def split_buckets(
    tasks: list[Task], today: date
) -> dict[str, list[Task]]:
    """Partition open tasks with a due date into the three urgency bands."""
    tomorrow = today + timedelta(days=1)
    buckets: dict[str, list[Task]] = {
        MODE_OVERDUE: [],
        MODE_DUE_TODAY: [],
        MODE_DUE_TOMORROW: [],
    }
    for task in tasks:
        if task.due_date is None:
            continue
        if task.due_date < today:
            buckets[MODE_OVERDUE].append(task)
        elif task.due_date == today:
            buckets[MODE_DUE_TODAY].append(task)
        elif task.due_date == tomorrow:
            buckets[MODE_DUE_TOMORROW].append(task)
    return buckets


def _parse_modes(modes: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    if not modes:
        return ALL_MODES
    out: list[str] = []
    for raw in modes:
        key = raw.strip().lower().replace("-", "_")
        if key not in BUCKET_HEADERS:
            raise ValueError(f"unknown urgency mode: {raw!r}")
        if key not in out:
            out.append(key)
    return tuple(out)


def card_candidates(
    buckets: dict[str, list[Task]],
    *,
    today: date,
    modes: tuple[str, ...],
    max_cards: int,
) -> list[Task]:
    """Oldest overdue person-first, then due-today; never tomorrow cards."""
    ordered: list[Task] = []
    for mode in CARD_MODES:
        if mode not in modes:
            continue
        ordered.extend(order_tasks_for_cards(buckets.get(mode, []), today=today))
    return ordered[: max(0, max_cards)]


async def run(
    store: TaskStore,
    sender: Sender,
    chat_id: int,
    *,
    today: date | None = None,
    now: datetime | None = None,
    modes: tuple[str, ...] | list[str] | None = None,
    max_cards: int = MAX_CANDIDATES,
    send_delay: float = SEND_DELAY,
) -> list[JobMessage]:
    """Send per-band digests, then at most one active card; queue the rest."""
    today = today or date.today()
    active_modes = _parse_modes(modes)
    open_with_due = [t for t in store.list_open_tasks(chat_id) if t.due_date is not None]
    buckets = split_buckets(open_with_due, today)

    sent: list[JobMessage] = []
    for mode in ALL_MODES:
        if mode not in active_modes:
            continue
        tasks = buckets[mode]
        if not tasks:
            continue
        header = BUCKET_HEADERS[mode]
        for text in format_person_list_messages(tasks, header=header, today=today):
            msg = JobMessage(chat_id, text)
            await deliver(store, sender, msg)
            sent.append(msg)
            if send_delay:
                await asyncio.sleep(send_delay)

    candidates = card_candidates(
        buckets, today=today, modes=active_modes, max_cards=max_cards
    )
    if not candidates:
        return sent

    chosen, queued = choose_cards(
        candidates, burst_limit=1, max_in_flight=MAX_IN_FLIGHT
    )
    for task in chosen:
        card = active_message(task, chat_id, today=today, now=now)
        await deliver(store, sender, card)
        sent.append(card)
        if send_delay:
            await asyncio.sleep(send_delay)
    save_queue(store, chat_id, [t.id for t in queued])

    # Candidates beyond max_cards are noted the same way as afternoon follow-up.
    all_cardable = card_candidates(
        buckets, today=today, modes=active_modes, max_cards=10_000
    )
    remaining = max(len(all_cardable) - len(candidates), 0)
    if remaining:
        notice = JobMessage(chat_id, format_followup_queue_notice(remaining))
        await deliver(store, sender, notice)
        sent.append(notice)
    return sent


class _DryRunSender:
    """Print what would be sent; never touches Telegram."""

    def __init__(self) -> None:
        self.messages: list[JobMessage] = []

    async def send(self, message: JobMessage):
        self.messages.append(message)
        print("---")
        print(message.text)
        if message.keyboard:
            labels = [" | ".join(label for label, _ in row) for row in message.keyboard]
            print("[buttons] " + "  /  ".join(labels))
        return None


def _modes_from_env_or_args(raw: str | None) -> tuple[str, ...] | None:
    text = (raw or os.environ.get("CHARBOT_URGENCY_MODES") or "").strip()
    if not text:
        return None
    return tuple(part.strip() for part in text.split(",") if part.strip())


async def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Urgency follow-up (overdue / due today / due tomorrow)."
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Print messages instead of sending (default: dry-run on).",
    )
    parser.add_argument(
        "--modes",
        default=None,
        help="Comma list: overdue,due_today,due_tomorrow (or CHARBOT_URGENCY_MODES).",
    )
    parser.add_argument("--max-cards", type=int, default=MAX_CANDIDATES)
    parser.add_argument(
        "--today",
        default=None,
        help="Override calendar day YYYY-MM-DD (tests / dry-run).",
    )
    args = parser.parse_args()

    from charbot.config import get_settings
    from charbot.store import store_from_settings

    from .cli import TelegramSender

    settings = get_settings()
    store = store_from_settings(settings)
    groups = settings.allowed_groups() or {-1002781646107}
    modes = _modes_from_env_or_args(args.modes)
    today = date.fromisoformat(args.today) if args.today else None

    if args.dry_run:
        sender: Sender = _DryRunSender()
        bot = None
    else:
        sender = TelegramSender(settings.require_token())
        bot = sender.bot
        await bot.initialize()

    try:
        for group_id in groups:
            sent = await run(
                store,
                sender,
                group_id,
                today=today,
                modes=modes,
                max_cards=args.max_cards,
                send_delay=0 if args.dry_run else SEND_DELAY,
            )
            if args.dry_run:
                print(f"# dry-run chat_id={group_id} messages={len(sent)}")
    finally:
        if bot:
            await bot.shutdown()


if __name__ == "__main__":
    asyncio.run(_cli())
