from __future__ import annotations

from datetime import date

from charbot.formatting import format_daily_plan
from charbot.store import TaskStore

from .common import JobMessage, Sender, deliver
from .followup import active_message


async def run(
    store: TaskStore, sender: Sender, chat_id: int, *, today: date | None = None
) -> list[JobMessage]:
    open_tasks = store.list_open_tasks(chat_id)
    decisions = []
    seen: set[int] = set()
    for task in [
        *store.list_overdue_tasks(chat_id, today=today),
        *store.list_unowned_open_tasks(chat_id),
    ]:
        if task.id not in seen:
            seen.add(task.id)
            decisions.append(task)
    plan = JobMessage(chat_id, format_daily_plan(open_tasks, decisions=len(decisions), today=today))
    sent = [plan]
    await deliver(store, sender, plan)
    if len(decisions) == 1:
        card = active_message(decisions[0], chat_id, today=today)
        await deliver(store, sender, card)
        sent.append(card)
    return sent


if __name__ == "__main__":
    import asyncio

    from .cli import run_cli

    asyncio.run(run_cli(run))
