from __future__ import annotations

import asyncio
from datetime import date

from charbot.formatting import format_person_list_messages
from charbot.store import TaskStore

from .common import LIST_SEND_DELAY, JobMessage, Sender, deliver, deliver_many
from .followup import active_message


async def run(
    store: TaskStore,
    sender: Sender,
    chat_id: int,
    *,
    today: date | None = None,
    send_delay: float = LIST_SEND_DELAY,
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
    if not open_tasks:
        texts = ["<b>برنامهٔ امروز</b>\n\nکاری در لیست نیست."]
    else:
        texts = format_person_list_messages(open_tasks, header="برنامهٔ امروز", today=today)
    plan_messages = [JobMessage(chat_id, text) for text in texts]
    sent = list(plan_messages)
    await deliver_many(store, sender, plan_messages, delay=send_delay)
    if len(decisions) == 1:
        if send_delay:
            await asyncio.sleep(send_delay)
        card = active_message(decisions[0], chat_id, today=today)
        await deliver(store, sender, card)
        sent.append(card)
    return sent


if __name__ == "__main__":
    from .cli import run_cli

    asyncio.run(run_cli(run))
