"""Scheduled follow-up: digest, then one task-specific active card."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from html import escape

from charbot.buttons import followup_question, question_buttons
from charbot.formatting import (
    format_followup_queue_notice,
    format_person_list_messages,
    format_task_question,
)
from charbot.members import chase_via, followup_addressee_fa, member_display_fa
from charbot.store import Task, TaskStore

from .common import JobMessage, Sender, deliver

MAX_CANDIDATES = 5
BURST_LIMIT = 3
SEND_DELAY = 0.6
QUEUE_KEY_PREFIX = "followup_queue:"


def queue_key(chat_id: int) -> str:
    return f"{QUEUE_KEY_PREFIX}{chat_id}"


def save_queue(store: TaskStore, chat_id: int, ids: list[int]) -> None:
    store.set_kv(queue_key(chat_id), json.dumps(list(ids)))


def ask_for_task(task: Task) -> str:
    """Active-card question text, including chase-via for Ghazal → Hamed."""
    title = escape((task.title or "").strip())
    key = task.assignee_key
    via = chase_via(key)
    if key and via and via != key:
        return followup_question(
            title,
            member_display_fa(key),
            via_fa=member_display_fa(via),
        )
    return followup_question(title, followup_addressee_fa(key))


def active_message(
    task: Task,
    chat_id: int,
    *,
    today: date | None = None,
    now: datetime | None = None,
) -> JobMessage:
    ask = ask_for_task(task)
    return JobMessage(
        chat_id,
        format_task_question(task, ask, today=today, now=now),
        question_buttons(ask, kind="fu", context=task.title or "", target_id=task.id),
    )


def dedupe_tasks(tasks: list[Task]) -> list[Task]:
    seen: set[int] = set()
    result: list[Task] = []
    for task in tasks:
        if task.id not in seen:
            seen.add(task.id)
            result.append(task)
    return result


# Back-compat alias used by tests.
_dedupe = dedupe_tasks


def burst_eligible(tasks: list[Task], limit: int) -> bool:
    named = [chase_via(task.assignee_key) for task in tasks if task.assignee_key]
    return 1 <= len(tasks) <= limit and len(named) == len(set(named)) and len(named) == len(tasks)


_burst_eligible = burst_eligible


def choose_cards(
    candidates: list[Task],
    *,
    burst_limit: int,
    max_in_flight: int | None = None,
) -> tuple[list[Task], list[Task]]:
    """Split candidates into cards to send now vs queue for later.

    ``max_in_flight`` forces sequential delivery (urgency uses 1). When
    omitted, the usual burst rule applies (≤``burst_limit`` distinct chase
    contacts).
    """
    if not candidates:
        return [], []
    if max_in_flight is not None:
        n = max(1, max_in_flight)
        return candidates[:n], candidates[n:]
    if burst_eligible(candidates, burst_limit):
        return candidates, []
    return candidates[:1], candidates[1:]


async def run(
    store: TaskStore,
    sender: Sender,
    chat_id: int,
    *,
    max_candidates: int = MAX_CANDIDATES,
    burst_limit: int = BURST_LIMIT,
    send_delay: float = SEND_DELAY,
    today: date | None = None,
    now: datetime | None = None,
) -> list[JobMessage]:
    """Send the shared follow-up surface; return exactly what was sent.

    The function is async because Telegram senders are async, but has no Telegram
    dependency and is straightforward to exercise with a fake sender.
    """
    return await _run(
        store,
        sender,
        chat_id,
        max_candidates=max_candidates,
        burst_limit=burst_limit,
        send_delay=send_delay,
        today=today,
        now=now,
    )


async def _run(
    store: TaskStore,
    sender: Sender,
    chat_id: int,
    *,
    max_candidates: int,
    burst_limit: int,
    send_delay: float,
    today: date | None,
    now: datetime | None,
) -> list[JobMessage]:
    overdue = store.list_overdue_tasks(chat_id, today=today)
    unowned = store.list_unowned_open_tasks(chat_id)
    watch = dedupe_tasks([*overdue, *unowned])
    if not watch:
        return []
    sent: list[JobMessage] = []
    for text in format_person_list_messages(watch, header="کارهای عقب‌افتاده", today=today):
        digest = JobMessage(chat_id, text)
        await deliver(store, sender, digest)
        sent.append(digest)
        if send_delay:
            await asyncio.sleep(send_delay)
    candidates = watch[:max_candidates]
    chosen, queued = choose_cards(candidates, burst_limit=burst_limit)
    for task in chosen:
        card = active_message(task, chat_id, today=today, now=now)
        await deliver(store, sender, card)
        sent.append(card)
        if send_delay:
            await asyncio.sleep(send_delay)
    save_queue(store, chat_id, [t.id for t in queued])
    remaining = max(len(watch) - len(candidates), 0)
    if remaining:
        notice = JobMessage(chat_id, format_followup_queue_notice(remaining))
        await deliver(store, sender, notice)
        sent.append(notice)
    return sent


from .cli import run_cli  # noqa: E402

if __name__ == "__main__":
    import asyncio

    asyncio.run(run_cli(run))
