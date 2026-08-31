"""Learn / check-in / glossary: directed speech never goes silent."""

from __future__ import annotations

from pathlib import Path

from charbot.glossary import (
    ack_learn,
    apply_text,
    apply_to_open_tasks,
    extract_glossary_entries,
    is_learn_utterance,
    upsert_entries,
)
from charbot.intent import SpeechActKind, classify_speech_act, may_create_task, must_reply
from charbot.store import TaskStore

GROUP = -1002781646107
JTI = "اسم کارفرما به انگلیسی JTI و به فارسی جی‌تی‌آی هست. درست استفاده‌ش کن. اوکی؟"


def test_jti_sentence_is_learn_and_must_reply() -> None:
    assert is_learn_utterance(JTI)
    act = classify_speech_act(JTI)
    assert act.kind == SpeechActKind.LEARN
    assert must_reply(act, JTI)
    assert not may_create_task(JTI)


def test_ok_question_is_checkin_not_confirm() -> None:
    act = classify_speech_act("اوکی؟")
    assert act.kind == SpeechActKind.CHECKIN
    assert must_reply(act, "اوکی؟")
    bare = classify_speech_act("اوکی")
    assert bare.kind == SpeechActKind.CONFIRM


def test_name_correction_class_not_one_sentence() -> None:
    samples = (
        "اسم کارفرما به انگلیسی Acme و به فارسی اکمی هست. درست استفاده کن. اوکی؟",
        "به انگلیسی SHEY و به فارسی شی. از این به بعد همین.",
        "اسم برند به فارسی شی و به انگلیسی SHEY هست",
        "متوجه شدی؟",
    )
    for text in samples:
        act = classify_speech_act(text)
        assert must_reply(act, text), text
        assert not may_create_task(text), text
        assert act.kind in (SpeechActKind.LEARN, SpeechActKind.CHECKIN), (text, act.kind)


def test_extract_and_rewrite_gti(tmp_path: Path) -> None:
    entries = extract_glossary_entries(JTI)
    assert entries
    assert entries[0].en == "JTI"
    assert "GTI" in entries[0].aliases
    assert "کارفرما JTI" in ack_learn(entries)
    assert apply_text("لیست مشتری GTI", entries) == "لیست مشتری JTI"
    store = TaskStore(tmp_path / "g.db")
    task = store.create_task(group_id=GROUP, title="لیست مشتری بالقوه GTI", assignee_key="saman")
    upsert_entries(store, entries)
    changed = apply_to_open_tasks(store, GROUP, entries)
    assert changed
    fresh = store.get_task(task.id, GROUP)
    assert fresh is not None
    assert "JTI" in fresh.title
    assert "GTI" not in fresh.title


def test_learn_handler_before_silent_question_branch() -> None:
    import inspect
    import charbot.bot as bot

    src = inspect.getsource(bot.handle_natural_language)
    assert src.find("SpeechActKind.LEARN") < src.find("classic_question")
    assert src.find("SpeechActKind.CHECKIN") < src.find("classic_question")
    assert "must_reply(act, raw)" in src
