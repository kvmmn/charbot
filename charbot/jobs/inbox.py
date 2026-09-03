from __future__ import annotations

from charbot.store import TaskStore


async def run(store: TaskStore, *, limit: int = 50) -> list[dict]:
    """Mark mirrored inbound messages processed; replies remain live-handler work."""
    rows = store.list_unprocessed_inbox(limit=limit)
    for row in rows:
        store.mark_inbox_processed(int(row["id"]))
    return rows


from .cli import run_cli  # noqa: E402

if __name__ == "__main__":
    import asyncio

    asyncio.run(run_cli(run, sends=False))
