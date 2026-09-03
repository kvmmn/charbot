from __future__ import annotations

from datetime import date

from charbot.report import berlin_today, render_period_report, week_bounds
from charbot.store import TaskStore

from .common import JobMessage, Sender, deliver


async def run(
    store: TaskStore, sender: Sender, chat_id: int, *, today: date | None = None
) -> list[JobMessage]:
    today = today or berlin_today()
    start, end = week_bounds(today)
    message = JobMessage(
        chat_id, render_period_report(store, chat_id, start, end, today=today, label="این هفته")
    )
    await deliver(store, sender, message)
    return [message]


from .cli import run_cli  # noqa: E402

if __name__ == "__main__":
    import asyncio

    asyncio.run(run_cli(run))
