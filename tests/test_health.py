"""Tests for FastAPI health endpoint (no live Telegram)."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000000000:TEST-TOKEN-FOR-CI")
os.environ.setdefault("BOT_MODE", "webhook")
os.environ.setdefault("WEBHOOK_URL", "https://example.test")
os.environ["DATABASE_URL"] = ""


@pytest.fixture
def client() -> TestClient:
    mock_ptb = MagicMock()
    mock_ptb.initialize = AsyncMock()
    mock_ptb.start = AsyncMock()
    mock_ptb.stop = AsyncMock()
    mock_ptb.shutdown = AsyncMock()
    mock_ptb.process_update = AsyncMock()
    mock_ptb.bot.set_webhook = AsyncMock()
    mock_ptb.bot.delete_webhook = AsyncMock()

    with patch("charbot.main.build_application", return_value=mock_ptb):
        from charbot.main import create_app

        with TestClient(create_app()) as test_client:
            yield test_client


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "charbot"}
