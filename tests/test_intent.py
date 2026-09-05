"""Speech-act gate: questions list work or roles; they never create junk tasks."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from charbot.bot import (
    has_pending_create_draft,
    interpret_work_or_followup,
    open_tasks_for,
    open_tasks_for_completion,
    save_pending_create_draft,
)
from charbot.intent import (
    SpeechActKind,
    classify_speech_act,
    is_completion_report,
    may_create_task,
    must_reply,
)
from charbot.members import chase_via
from charbot.nlp import NLIntent, parse_natural_language
from charbot.store import TaskStore
from charbot.understand import extract_task

TODAY = date(2026, 8, 31)
GROUP = -1001111111111


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
        username="kawe_tg",
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
        username="kawe_tg",
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


DONE_1 = "من بررسی قرارداد رو انجام دادم و فرستادم برای حامد و کارم تموم شد"
DONE_2 = "موعدش رو خودت گفته بودی امروزه بوده. الان انجام و تکمیل و تحویل شد."
DONE_3 = "دارم گزارش کار میدم. نه این که تسک یا فعالیت جدید معرفی کنم."


def _kawe_store(tmp_path: Path) -> TaskStore:
    store = TaskStore(tmp_path / "done.db")
    store.upsert_user_mapping(
        telegram_user_id=42,
        member_key="kawe",
        username="kawe_tg",
        display_name="Kawe",
    )
    return store


@pytest.mark.parametrize(
    "text",
    [
        DONE_1,
        DONE_2,
        DONE_3,
        "کارم تموم شد",
        "انجام شد",
        "تکمیل شد",
        "تحویل شد",
        "کارم خلاص شد",
        "فرستادم",
        "I finished it, done",
        "the work is delivered",
    ],
)
def test_completion_reports_are_not_create(text: str) -> None:
    act = classify_speech_act(text, speaker_key="kawe")
    assert act.kind == SpeechActKind.REPORT_DONE, text
    assert not may_create_task(text), text
    assert is_completion_report(text)
    assert must_reply(act, text)
    parsed = parse_natural_language(text, today=TODAY, speaker_key="kawe")
    assert parsed.intent != NLIntent.CREATE_TASK, text
    understood = extract_task(text, speaker_key="kawe", today=TODAY)
    assert not understood.title
    assert understood.ask is None


def test_imperative_create_not_stolen_by_done() -> None:
    text = "قرارداد حامد را تا فردا بررسی کن"
    assert classify_speech_act(text).kind == SpeechActKind.CREATE_TASK
    assert may_create_task(text)
    assert not is_completion_report(text)


def test_period_report_stays_report() -> None:
    text = "گزارش این هفته"
    assert classify_speech_act(text).kind == SpeechActKind.REPORT
    assert not is_completion_report(text)


def test_done_report_marks_matching_kawe_contract_task(tmp_path: Path) -> None:
    store = _kawe_store(tmp_path)
    task = store.create_task(
        group_id=GROUP,
        title="بررسی فایل تدوین‌شده قرارداد حامد",
        assignee_key="kawe",
        due_date=TODAY,
    )
    store.create_task(
        group_id=GROUP,
        title="لوگو اینستا",
        assignee_key="saman",
        due_date=TODAY,
    )
    result = interpret_work_or_followup(
        store,
        chat_id=GROUP,
        raw=DONE_1,
        speaker_key="kawe",
        speaker_user_id=42,
        today=TODAY,
    )
    assert result.created is False
    assert result.completed is True
    assert result.task is not None
    assert result.task.id == task.id
    fetched = store.get_task(task.id, GROUP)
    assert fetched is not None
    assert fetched.completed_at is not None
    assert "تکمیل" in (result.reply or "")
    open_ids = [t.id for t in store.list_open_tasks(GROUP)]
    assert task.id not in open_ids


def test_pending_create_draft_abandoned_on_done_report(tmp_path: Path) -> None:
    store = _kawe_store(tmp_path)
    store.create_task(
        group_id=GROUP,
        title="بررسی فایل تدوین‌شده قرارداد حامد",
        assignee_key="kawe",
        due_date=TODAY,
    )
    save_pending_create_draft(store, "kawe", "بررسی قرارداد")
    assert has_pending_create_draft(store, "kawe")
    result = interpret_work_or_followup(
        store,
        chat_id=GROUP,
        raw=DONE_2,
        speaker_key="kawe",
        speaker_user_id=42,
        today=TODAY,
    )
    assert not has_pending_create_draft(store, "kawe")
    assert result.created is False
    assert not may_create_task(DONE_2)
    assert "موعد کی است" not in (result.reply or "")
    assert "مسئول کیست" not in (result.reply or "")


def test_done_report_several_matches_asks_with_buttons(tmp_path: Path) -> None:
    store = _kawe_store(tmp_path)
    store.create_task(
        group_id=GROUP, title="بررسی قرارداد الف", assignee_key="kawe", due_date=TODAY
    )
    store.create_task(group_id=GROUP, title="بررسی قرارداد ب", assignee_key="kawe", due_date=TODAY)
    result = interpret_work_or_followup(
        store,
        chat_id=GROUP,
        raw="بررسی قرارداد تموم شد",
        speaker_key="kawe",
        speaker_user_id=42,
        today=TODAY,
    )
    assert result.created is False
    assert result.completed is False
    rows = result.button_rows or []
    n = sum(len(row) for row in rows)
    assert 2 <= n <= 4
    assert store.list_open_tasks(GROUP)
    assert "تمام" in (result.reply or "")


def test_done_report_no_match_never_creates(tmp_path: Path) -> None:
    store = _kawe_store(tmp_path)
    other = store.create_task(
        group_id=GROUP, title="لوگو اینستا", assignee_key="kawe", due_date=TODAY
    )
    result = interpret_work_or_followup(
        store,
        chat_id=GROUP,
        raw=DONE_3,
        speaker_key="kawe",
        speaker_user_id=42,
        today=TODAY,
    )
    assert result.created is False
    assert result.completed is False
    assert store.get_task(other.id, GROUP).completed_at is None
    rows = result.button_rows or []
    labels = [label for row in rows for label, _data in row]
    assert any("لوگو" in label for label in labels)
    assert store.list_open_tasks(GROUP)


def _board_store(tmp_path: Path) -> TaskStore:
    store = TaskStore(tmp_path / "chase-done.db")
    store.upsert_user_mapping(
        telegram_user_id=42, member_key="kawe", username="kawe_tg", display_name="Kawe"
    )
    store.upsert_user_mapping(
        telegram_user_id=2, member_key="hamed", username="hamed", display_name="Hamed"
    )
    store.upsert_user_mapping(
        telegram_user_id=3, member_key="saman", username="saman", display_name="Saman"
    )
    return store


def test_open_tasks_for_completion_includes_chase_via_and_kawe(tmp_path: Path) -> None:
    store = _board_store(tmp_path)
    logo = store.create_task(
        group_id=GROUP, title="اجرای سه لوگو", assignee_key="ghazal", due_date=TODAY
    )
    minutes = store.create_task(
        group_id=GROUP, title="صورتجلسه هیئت مدیره", assignee_key="hamed", due_date=TODAY
    )
    flight = store.create_task(
        group_id=GROUP, title="بلیط پرواز مشهد", assignee_key="saman", due_date=TODAY
    )
    hamed_ids = {t.id for t in open_tasks_for_completion(store, GROUP, "hamed")}
    assert hamed_ids == {logo.id, minutes.id}
    saman_ids = {t.id for t in open_tasks_for_completion(store, GROUP, "saman")}
    assert saman_ids == {flight.id}
    kawe_ids = {t.id for t in open_tasks_for_completion(store, GROUP, "kawe")}
    assert kawe_ids == {logo.id, minutes.id, flight.id}
    assert [t.id for t in open_tasks_for(store, GROUP, "hamed")] == [minutes.id]
    assert chase_via("ghazal") == "hamed"
    assert chase_via("saman") == "saman"


def test_hamed_done_report_marks_ghazal_logo(tmp_path: Path) -> None:
    store = _board_store(tmp_path)
    logo = store.create_task(
        group_id=GROUP, title="اجرای سه لوگو", assignee_key="ghazal", due_date=TODAY
    )
    store.create_task(
        group_id=GROUP, title="صورتجلسه هیئت مدیره", assignee_key="hamed", due_date=TODAY
    )
    result = interpret_work_or_followup(
        store,
        chat_id=GROUP,
        raw="لوگو تموم شد",
        speaker_key="hamed",
        speaker_user_id=2,
        today=TODAY,
    )
    assert result.created is False
    assert result.completed is True
    assert result.task is not None
    assert result.task.id == logo.id
    fetched = store.get_task(logo.id, GROUP)
    assert fetched is not None
    assert fetched.assignee_key == "ghazal"
    assert fetched.completed_at is not None
    assert "تکمیل" in (result.reply or "")


def test_kawe_done_report_marks_ghazal_logo(tmp_path: Path) -> None:
    store = _board_store(tmp_path)
    logo = store.create_task(
        group_id=GROUP, title="اجرای سه لوگو", assignee_key="ghazal", due_date=TODAY
    )
    store.create_task(group_id=GROUP, title="بلیط پرواز مشهد", assignee_key="saman", due_date=TODAY)
    result = interpret_work_or_followup(
        store,
        chat_id=GROUP,
        raw="لوگو تموم شد",
        speaker_key="kawe",
        speaker_user_id=42,
        today=TODAY,
    )
    assert result.created is False
    assert result.completed is True
    assert result.task is not None
    assert result.task.id == logo.id
    fetched = store.get_task(logo.id, GROUP)
    assert fetched is not None
    assert fetched.assignee_key == "ghazal"
    assert fetched.completed_at is not None


def test_saman_done_report_does_not_steal_ghazal_logo(tmp_path: Path) -> None:
    store = _board_store(tmp_path)
    logo = store.create_task(
        group_id=GROUP, title="اجرای سه لوگو", assignee_key="ghazal", due_date=TODAY
    )
    flight = store.create_task(
        group_id=GROUP, title="بلیط پرواز مشهد", assignee_key="saman", due_date=TODAY
    )
    result = interpret_work_or_followup(
        store,
        chat_id=GROUP,
        raw="لوگو تموم شد",
        speaker_key="saman",
        speaker_user_id=3,
        today=TODAY,
    )
    assert result.created is False
    assert result.completed is False
    assert store.get_task(logo.id, GROUP).completed_at is None
    assert store.get_task(flight.id, GROUP).completed_at is None


def test_hamed_done_report_still_completes_own_task(tmp_path: Path) -> None:
    store = _board_store(tmp_path)
    store.create_task(group_id=GROUP, title="اجرای سه لوگو", assignee_key="ghazal", due_date=TODAY)
    minutes = store.create_task(
        group_id=GROUP, title="صورتجلسه هیئت مدیره", assignee_key="hamed", due_date=TODAY
    )
    result = interpret_work_or_followup(
        store,
        chat_id=GROUP,
        raw="صورتجلسه تموم شد",
        speaker_key="hamed",
        speaker_user_id=2,
        today=TODAY,
    )
    assert result.created is False
    assert result.completed is True
    assert result.task is not None
    assert result.task.id == minutes.id
    assert store.get_task(minutes.id, GROUP).completed_at is not None
    logo = next(t for t in store.list_open_tasks(GROUP) if t.assignee_key == "ghazal")
    assert logo.completed_at is None
