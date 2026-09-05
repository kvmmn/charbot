"""Tests for command and natural-language parsing."""

from __future__ import annotations

from datetime import date

import pytest

from charbot.members import resolve_member_name
from charbot.nlp import (
    NLIntent,
    parse_assign_command,
    parse_command_args,
    parse_date,
    parse_done_command,
    parse_due_command,
    parse_natural_language,
    parse_task_command,
)


def test_resolve_member_aliases() -> None:
    assert resolve_member_name("Kaveh") == "kawe"
    assert resolve_member_name("kawe") == "kawe"
    assert resolve_member_name("Saman") == "saman"
    assert resolve_member_name("Mohammadreza") == "mohammadreza"
    assert resolve_member_name("unknown") is None


def test_parse_command_args() -> None:
    cmd, args = parse_command_args("/task Ship report")
    assert cmd == "task"
    assert args == ["Ship", "report"]

    cmd2, args2 = parse_command_args("/help@YourBot")
    assert cmd2 == "help"
    assert args2 == []


def test_parse_task_command() -> None:
    assert parse_task_command(["Send", "invoice"]) == "Send invoice"
    assert parse_task_command([]) is None


def test_parse_assign_command() -> None:
    tid, key = parse_assign_command(["3", "Kawe"])
    assert tid == 3
    assert key == "kawe"


def test_parse_due_command() -> None:
    tid, due = parse_due_command(["5", "2026-03-15"])
    assert tid == 5
    assert due == date(2026, 3, 15)

    _, due2 = parse_due_command(["5", "tomorrow"])
    assert due2 == parse_date("tomorrow")


def test_parse_done_command() -> None:
    assert parse_done_command(["7"]) == 7
    assert parse_done_command([]) is None


@pytest.mark.parametrize(
    "text,expected_intent",
    [
        ("task: Prepare quarterly report", NLIntent.CREATE_TASK),
        ("تسک: ارسال فاکتور", NLIntent.CREATE_TASK),
        ("assign 3 Kawe", NLIntent.ASSIGN),
        ("done 2", NLIntent.MARK_DONE),
        ("open tasks", NLIntent.LIST_OPEN),
        ("overdue", NLIntent.LIST_OVERDUE),
        ("hello team", NLIntent.NONE),
    ],
)
def test_natural_language_intents(text: str, expected_intent: NLIntent) -> None:
    parsed = parse_natural_language(text)
    assert parsed.intent == expected_intent


def test_parse_date_variants() -> None:
    today = date(2026, 8, 29)
    assert parse_date("today", today=today) == today
    assert parse_date("امروز", today=today) == today
    assert parse_date("tomorrow", today=today) == date(2026, 8, 30)
    assert parse_date("2026-12-01", today=today) == date(2026, 12, 1)
    assert parse_date("15/03", today=today) == date(2026, 3, 15)


@pytest.mark.parametrize(
    "text,expected_intent",
    [
        ("کارهای من چی بودن؟", NLIntent.LIST_TASKS),
        ("مرسی. کارهای حامد چیان؟", NLIntent.LIST_TASKS),
        ("کارهای باز", NLIntent.LIST_OPEN),
        ("نقش حامد چیه؟", NLIntent.QUERY_ROLE),
        ("قرارداد حامد را تا فردا بررسی کن", NLIntent.CREATE_TASK),
    ],
)
def test_speech_act_nl_intents(text: str, expected_intent: NLIntent) -> None:
    parsed = parse_natural_language(text, speaker_key="kawe")
    assert parsed.intent == expected_intent
