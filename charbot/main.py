"""Application entrypoints: polling, webhook (FastAPI), and CLI."""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, Response
from telegram import Update

from charbot.bot import build_application
from charbot.config import BotMode, Settings, get_settings
from charbot.store import store_from_settings

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("charbot")


class RedactSecretsFilter(logging.Filter):
    """Keep bot tokens out of httpx/Telegram URL logs."""

    _bot_url = re.compile(r"https?://api\.telegram\.org/bot[^/\s]+")
    _bot_path = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")
    _bearer = re.compile(r"(Bearer )\S+", re.I)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
            record.msg = self.scrub(text)
            record.args = ()
        except Exception:
            pass
        return True

    @classmethod
    def scrub(cls, value: Any) -> Any:
        if not isinstance(value, str):
            value = str(value)
        value = cls._bot_url.sub("https://api.telegram.org/bot<redacted>", value)
        value = cls._bot_path.sub("/bot<redacted>", value)
        value = cls._bearer.sub(r"\1<redacted>", value)
        return value


class RedactSecretsFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return RedactSecretsFilter.scrub(super().format(record))


def install_log_redaction() -> None:
    filt = RedactSecretsFilter()
    fmt = RedactSecretsFormatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.addFilter(filt)
    for handler in root.handlers:
        handler.addFilter(filt)
        handler.setFormatter(fmt)
    for name in ("httpx", "httpcore", "telegram", "telegram.ext", "charbot"):
        logging.getLogger(name).addFilter(filt)


install_log_redaction()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = store_from_settings(settings)
    ptb_app = build_application(settings, store)

    @asynccontextmanager
    async def lifespan(api: FastAPI) -> AsyncIterator[None]:
        await ptb_app.initialize()
        await ptb_app.start()
        if settings.bot_mode == BotMode.WEBHOOK:
            webhook_url = settings.webhook_url.rstrip("/") + settings.webhook_path
            await ptb_app.bot.set_webhook(
                url=webhook_url,
                secret_token=settings.webhook_secret or None,
                drop_pending_updates=False,
            )
            logger.info("Webhook registered at %s", webhook_url)
        api.state.ptb_app = ptb_app
        yield
        if settings.bot_mode == BotMode.WEBHOOK:
            await ptb_app.bot.delete_webhook(drop_pending_updates=False)
        await ptb_app.stop()
        await ptb_app.shutdown()

    api = FastAPI(title="charbot", version="1.0.0", lifespan=lifespan)

    @api.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "charbot"}

    @api.post(settings.webhook_path)
    async def telegram_webhook(request: Request) -> Response:
        if settings.webhook_secret:
            token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
            if token != settings.webhook_secret:
                return Response(status_code=403)

        data = await request.json()
        update = Update.de_json(data, ptb_app.bot)
        await ptb_app.process_update(update)
        return Response(status_code=200)

    return api


def run_polling(settings: Settings) -> None:
    store = store_from_settings(settings)
    app = build_application(settings, store)
    logger.info("Starting charbot in polling mode")
    app.run_polling(drop_pending_updates=False)


def run_webhook(settings: Settings) -> None:
    if not settings.webhook_url:
        raise RuntimeError("WEBHOOK_URL is required for webhook mode")
    api = create_app(settings)
    logger.info("Starting charbot webhook server on %s:%s", settings.host, settings.port)
    uvicorn.run(api, host=settings.host, port=settings.port, log_level="info")


def main() -> None:
    parser = argparse.ArgumentParser(description="charbot — Telegram coordinator")
    parser.add_argument(
        "--mode",
        choices=["polling", "webhook"],
        help="Override BOT_MODE env var",
    )
    args = parser.parse_args()
    settings = get_settings()

    if args.mode:
        settings = settings.model_copy(update={"bot_mode": BotMode(args.mode)})

    try:
        settings.require_token()
    except RuntimeError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    if settings.bot_mode == BotMode.POLLING:
        run_polling(settings)
    else:
        run_webhook(settings)


if __name__ == "__main__":
    main()
