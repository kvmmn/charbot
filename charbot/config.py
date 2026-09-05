"""Application configuration from environment variables."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotMode(StrEnum):
    POLLING = "polling"
    WEBHOOK = "webhook"


def parse_tg_usernames(raw: str) -> dict[str, str]:
    """Parse CHARBOT_TG_USERNAMES: key:username,key:username."""
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        key, username = part.split(":", 1)
        key = key.strip()
        username = username.strip().lstrip("@")
        if key and username:
            out[key] = username
    return out


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_group_id: int | None = Field(default=None, alias="TELEGRAM_GROUP_ID")
    telegram_allowed_group_ids: str = Field(default="", alias="TELEGRAM_ALLOWED_GROUP_IDS")

    bot_mode: BotMode = Field(default=BotMode.POLLING, alias="BOT_MODE")
    webhook_url: str = Field(default="", alias="WEBHOOK_URL")
    webhook_path: str = Field(default="/telegram/webhook", alias="WEBHOOK_PATH")
    webhook_secret: str = Field(default="", alias="WEBHOOK_SECRET")

    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8081, alias="PORT")

    database_path: Path = Field(default=Path("data/charbot.db"), alias="DATABASE_PATH")
    database_url: str = Field(default="", alias="DATABASE_URL")
    followup_interval_hours: int = Field(default=24, alias="FOLLOWUP_INTERVAL_HOURS")
    followup_enabled: bool = Field(default=True, alias="FOLLOWUP_ENABLED")

    charbot_tg_usernames: str = Field(default="", alias="CHARBOT_TG_USERNAMES")

    def allowed_groups(self) -> set[int]:
        ids: set[int] = set()
        if self.telegram_group_id is not None:
            ids.add(self.telegram_group_id)
        if self.telegram_allowed_group_ids.strip():
            for part in self.telegram_allowed_group_ids.split(","):
                part = part.strip()
                if part:
                    ids.add(int(part))
        return ids

    def tg_usernames(self) -> dict[str, str]:
        return parse_tg_usernames(self.charbot_tg_usernames)

    def require_token(self) -> str:
        if not self.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
        return self.telegram_bot_token


def get_settings() -> Settings:
    return Settings()


def telegram_username(key: str | None) -> str | None:
    """Lookup a member key's Telegram @username from CHARBOT_TG_USERNAMES."""
    if not key:
        return None
    return get_settings().tg_usernames().get(key)
