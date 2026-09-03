"""Scheduled follow-up: digest, then one task-specific active card."""

from __future__ import annotations

import asyncio
import json
from datetime import date

from charbot.buttons import followup_question, question_buttons
from charbot.formatting import (
    format_followup_queue_notice,
    format_task_digest,
    format_task_question,
)
from charbot.members import member_display_fa
from charbot.store import Task, TaskStore

from .common import JobMessage, Sender, deliver

MAX_CANDIDATES = 5
BURST_LIMIT = 3
SEND_DELAY = 0.6


def active_message(task: Task, chat_id: int, *, today: date | None = None) -> JobMessage:
    ask = followup_question(
        task.title or "", member_display_fa(task.assignee_key) if task.assignee_key else None
    )
    return JobMessage(
        chat_id,
        format_task_question(task, ask, today=today),
        question_buttons(ask, kind="fu", context=task.title or "", target_id=task.id),
    )


def _dedupe(tasks: list[Task]) -> list[Task]:
    seen: set[int] = set()
    result: list[Task] = []
    for task in tasks:
        if task.id not in seen:
            seen.add(task.id)
            result.append(task)
    return result


def _burst_eligible(tasks: list[Task], limit: int) -> bool:
    named = [task.assignee_key for task in tasks if task.assignee_key]
    return 1 <= len(tasks) <= limit and len(named) == len(set(named)) and len(named) == len(tasks)


async def run(
    store: TaskStore,
    sender: Sender,
    chat_id: int,
    *,
    max_candidates: int = MAX_CANDIDATES,
    burst_limit: int = BURST_LIMIT,
    send_delay: float = SEND_DELAY,
    today: date | None = None,
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
) -> list[JobMessage]:
    overdue = store.list_overdue_tasks(chat_id, today=today)
    unowned = store.list_unowned_open_tasks(chat_id)
    watch = _dedupe([*overdue, *unowned])
    if not watch:
        return []
    sent: list[JobMessage] = []
    digest = JobMessage(chat_id, format_task_digest(watch, header="کارهای عقب‌افتاده", today=today))
    await deliver(store, sender, digest)
    sent.append(digest)
    if send_delay:
        await asyncio.sleep(send_delay)
    candidates = watch[:max_candidates]
    if _burst_eligible(candidates, burst_limit):
        chosen = candidates
        queued: list[Task] = []
    else:
        chosen, queued = candidates[:1], candidates[1:]
    for task in chosen:
        card = active_message(task, chat_id, today=today)
        await deliver(store, sender, card)
        sent.append(card)
        if send_delay:
            await asyncio.sleep(send_delay)
    if queued:
        store.set_kv(f"followup_queue:{chat_id}", json.dumps([t.id for t in queued]))
    else:
        store.set_kv(f"followup_queue:{chat_id}", "[]")
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
