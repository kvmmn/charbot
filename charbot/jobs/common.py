from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from charbot.store import TaskStore

LIST_SEND_DELAY = 0.5


@dataclass(frozen=True)
class JobMessage:
    chat_id: int
    text: str
    keyboard: list[list[tuple[str, str]]] | None = None


class Sender(Protocol):
    async def send(self, message: JobMessage) -> Any: ...


def _message_id(result: Any) -> int | None:
    if isinstance(result, int):
        return result
    value = getattr(result, "message_id", None)
    return int(value) if value is not None else None


async def deliver(store: TaskStore, sender: Sender, message: JobMessage) -> Any:
    result = await sender.send(message)
    message_id = _message_id(result)
    if hasattr(store, "log_conversation"):
        store.log_conversation(
            chat_id=message.chat_id,
            direction="out",
            kind="text",
            text=message.text,
            telegram_message_id=message_id,
        )
    return result


async def deliver_many(
    store: TaskStore,
    sender: Sender,
    messages: list[JobMessage],
    *,
    delay: float = LIST_SEND_DELAY,
) -> list[Any]:
    """Send read-only messages in order, paced to avoid Telegram 429s."""
    results = []
    for i, message in enumerate(messages):
        results.append(await deliver(store, sender, message))
        if delay and i < len(messages) - 1:
            await asyncio.sleep(delay)
    return results
