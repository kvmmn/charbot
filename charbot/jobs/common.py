from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from charbot.store import TaskStore


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
