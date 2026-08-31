"""Colleague loop: directed speech always replies; tools stay allowlisted."""

from __future__ import annotations

from pathlib import Path

from charbot.agent import ALLOWED_TOOLS, run_colleague
from charbot.intent import SpeechActKind, classify_speech_act
from charbot.store import TaskStore

GROUP = -1002781646107
JTI = "اسم کارفرما به انگلیسی JTI و به فارسی جی‌تی‌آی هست. درست استفاده‌ش کن. اوکی؟"


def test_learn_replies_and_rewrites(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "a.db")
    store.create_task(group_id=GROUP, title="لیست مشتری GTI", assignee_key="saman")
    act = classify_speech_act(JTI)
    result = run_colleague(store, group_id=GROUP, text=JTI, act=act, speaker_key="kawe")
    assert act.kind == SpeechActKind.LEARN
    assert result.reply
    assert "JTI" in result.reply
    assert "learn_glossary" in result.tools
    assert result.source == "heuristic"
    title = store.list_open_tasks(GROUP)[0].title
    assert "JTI" in title
    assert "GTI" not in title


def test_checkin_ok_question(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "c.db")
    act = classify_speech_act("اوکی؟")
    result = run_colleague(store, group_id=GROUP, text="اوکی؟", act=act)
    assert act.kind == SpeechActKind.CHECKIN
    assert result.reply
    assert result.tools == ["reply"]


def test_tools_are_allowlisted() -> None:
    assert ALLOWED_TOOLS == {"reply", "learn_glossary", "ask"}
    assert "create_task" not in ALLOWED_TOOLS
