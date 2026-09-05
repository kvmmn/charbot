from __future__ import annotations

from collections.abc import Callable

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from charbot.config import get_settings
from charbot.store import store_from_settings

from .common import JobMessage


class TelegramSender:
    def __init__(self, token: str):
        self.bot = Bot(token)

    async def send(self, message: JobMessage):
        markup = None
        if message.keyboard:
            markup = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(label, callback_data=data) for label, data in row]
                    for row in message.keyboard
                ]
            )
        return await self.bot.send_message(
            chat_id=message.chat_id, text=message.text, parse_mode="HTML", reply_markup=markup
        )


async def run_cli(job: Callable, *, sends: bool = True) -> None:
    settings = get_settings()
    store = store_from_settings(settings)
    groups = settings.allowed_groups()
    if not groups:
        raise SystemExit(
            "No Telegram groups configured. "
            "Set TELEGRAM_GROUP_ID or TELEGRAM_ALLOWED_GROUP_IDS before sending."
        )
    sender = TelegramSender(settings.require_token()) if sends else None
    bot = sender.bot if sender else None
    if bot:
        await bot.initialize()
    try:
        for group_id in groups:
            if sender:
                await job(store, sender, group_id)
            else:
                await job(store)
    finally:
        if bot:
            await bot.shutdown()
