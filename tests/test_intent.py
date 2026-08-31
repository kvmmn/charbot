"""Speech-act gate: questions list work or roles; they never create junk tasks."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from charbot.bot import interpret_work_or_followup
from charbot.intent import SpeechActKind, classify_speech_act, may_create_task
from charbot.nlp import NLIntent, parse_natural_language
from charbot.store import TaskStore

TODAY = date(2026, 8, 31)
GROUP = -1002781646107


@pytest.mark.parametrize(
    "text,kind,person,for_speaker,board",
    [
        ("کارهای من چی بودن؟", SpeechActKind.LIST_TASKS, None, True, False),
        ("کارهام چیه", SpeechActKind.LIST_TASKS, None, True, False),
        ("تسک‌های من", SpeechActKind.LIST_TASKS, None, True, False),
        ("کارهای باز من", SpeechActKind.LIST_TASKS, None, True, False),
        ("چی به من سپردی", SpeechActKind.LIST_TASKS, None, True, False),
        ("لیست کارهام", SpeechActKind.LIST_TASKS, None, True, False),
        ("مرسی. کارهای حامد چیان؟", SpeechActKind.LIST_TASKS, "hamed", False, False),
        ("کارهای سامان چی؟", SpeechActKind.LIST_TASKS, "saman", False, False),
        ("کارای سامان چیه", SpeechActKind.LIST_TASKS, "saman", False, False),
        ("کارهای حامد", SpeechActKind.LIST_TASKS, "hamed", False, False),
        ("کارهای باز", SpeechActKind.LIST_TASKS, None, False, True),
        ("نقش حامد چیه؟", SpeechActKind.QUERY_ROLE, "hamed", False, False),
        ("قرارداد حامد را تا فردا بررسی کن", SpeechActKind.CREATE_TASK, "hamed", False, False),
        ("task: Prepare quarterly report", SpeechActKind.CREATE_TASK, None, False, False),
        ("تسک: ارسال فاکتور", SpeechActKind.CREATE_TASK, None, False, False),
    ],
)
def test_speech_act_table(text, kind, person, for_speaker, board) -> None:
    act = classify_speech_act(text, speaker_key="kawe")
    assert act.kind == kind
    assert act.person_key == person
    assert act.for_speaker is for_speaker
    assert act.board_open is board


def test_questions_never_create() -> None:
    for text in (
        "کارهای من چی بودن؟",
        "مرسی. کارهای حامد چیان؟",
        "کارهای سامان چی؟",
        "کارای سامان چیه",
        "کارهای باز",
        "نقش حامد چیه؟",
        "کارهام چیه",
        "تسک‌های من",
        "چی به من سپردی",
        "لیست کارهام",
        "کارهای باز من",
    ):
        assert not may_create_task(text), text


def test_imperative_still_creates() -> None:
    assert may_create_task("قرارداد حامد را تا فردا بررسی کن")
    assert may_create_task("من باید فایل قرارداد رو بررسی کنم تا فردا")


@pytest.mark.parametrize(
    "text,intent,assignee",
    [
        ("کارهای من چی بودن؟", NLIntent.LIST_TASKS, "kawe"),
        ("مرسی. کارهای حامد چیان؟", NLIntent.LIST_TASKS, "hamed"),
        ("کارهای سامان چی؟", NLIntent.LIST_TASKS, "saman"),
        ("کارهای باز", NLIntent.LIST_OPEN, None),
        ("نقش حامد چیه؟", NLIntent.QUERY_ROLE, "hamed"),
        ("قرارداد حامد را تا فردا بررسی کن", NLIntent.CREATE_TASK, None),
    ],
)
def test_parse_natural_language_table(text, intent, assignee) -> None:
    parsed = parse_natural_language(text, today=TODAY, speaker_key="kawe")
    assert parsed.intent == intent
    if assignee and intent != NLIntent.CREATE_TASK:
        assert parsed.assignee_key == assignee
    if intent == NLIntent.CREATE_TASK:
        assert parsed.title
        assert "های من" not in (parsed.title or "")
        assert not (parsed.title or "").startswith("های ")


def test_kar_prefix_does_not_strip_questions() -> None:
    parsed = parse_natural_language("کارهای من چی بودن؟", speaker_key="kawe")
    assert parsed.intent != NLIntent.CREATE_TASK
    assert parsed.title is None


def test_interpret_list_questions_do_not_insert(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "intent.db")
    store.upsert_user_mapping(
        telegram_user_id=42,
        member_key="kawe",
        username="kvmmn",
        display_name="Kawe",
    )
    for text in (
        "کارهای من چی بودن؟",
        "مرسی. کارهای حامد چیان؟",
        "نقش حامد چیه؟",
    ):
        result = interpret_work_or_followup(
            store,
            chat_id=GROUP,
            raw=text,
            speaker_key="kawe",
            speaker_user_id=42,
            today=TODAY,
        )
        assert result.created is False, text
        assert result.task is None, text
        titles = [t.title for t in store.list_open_tasks(GROUP)]
        assert titles == [], text


def test_interpret_still_creates_imperative(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "create.db")
    store.upsert_user_mapping(
        telegram_user_id=42,
        member_key="kawe",
        username="kvmmn",
        display_name="Kawe",
    )
    result = interpret_work_or_followup(
        store,
        chat_id=GROUP,
        raw="قرارداد حامد را تا فردا بررسی کن",
        speaker_key="kawe",
        speaker_user_id=42,
        today=TODAY,
    )
    assert result.created is True
    assert result.task is not None
    assert "بررسی" in (result.task.title or "") or "قرارداد" in (result.task.title or "")


def test_gate_runs_before_role_dump() -> None:
    import inspect

    import charbot.bot as bot

    src = inspect.getsource(bot.handle_natural_language)
    assert src.find("classify_speech_act") < src.find("classic_question")
    assert src.find("SpeechActKind.LIST_TASKS") < src.find("classic_question")
    assert src.find("SpeechActKind.QUERY_ROLE") < src.find("classic_question")
    from charbot import nlp
    nsrc = inspect.getsource(nlp.parse_natural_language)
    assert nsrc.find("classify_speech_act") < nsrc.find("understood = extract_task")
    assert nsrc.find("SpeechActKind.LIST_TASKS") < nsrc.find("understood = extract_task")


def test_question_shaped_title_refused(tmp_path) -> None:
    from charbot.intent import is_question_shaped_title
    from charbot.store import TaskStore

    assert is_question_shaped_title("های سامان چی")
    assert is_question_shaped_title("کارهای سامان چی؟")
    assert not is_question_shaped_title("امضای صورتجلسه فرودگاه")
    store = TaskStore(tmp_path / "t.db")
    try:
        store.create_task(group_id=GROUP, title="های سامان چی", assignee_key="saman")
        raise AssertionError("store must refuse remnant titles")
    except ValueError as exc:
        assert "question-shaped" in str(exc)
