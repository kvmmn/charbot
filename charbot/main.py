"""Application entrypoints: polling, webhook (FastAPI), and CLI."""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

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
    parser = argparse.ArgumentParser(description="charbot — Chaharsotoon coordinator")
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
