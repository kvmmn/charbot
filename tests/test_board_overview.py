"""Board overview intent: Kaveh-style group asks must fire instantly."""

from __future__ import annotations

from pathlib import Path

from charbot.bot import (
    _is_question,
    format_board_overview,
    is_board_overview,
)
from charbot.store import TaskStore

KAVEH_SENTENCE = (
    "یه بار در مورد نقش تک‌تک ما چهار نفر در شرکت نمونه "
    "و این که هر کدوم چیکار می‌کنیم کامل توضیح بده"
)


def test_kaveh_sentence_is_board_overview() -> None:
    assert is_board_overview(KAVEH_SENTENCE)
    assert _is_question(KAVEH_SENTENCE)
    assert "؟" not in KAVEH_SENTENCE
    assert "thecharbot" not in KAVEH_SENTENCE.lower()
    assert "چاربات" not in KAVEH_SENTENCE


def test_imperative_asks_are_questions() -> None:
    assert _is_question("نقش‌ها را توضیح بده")
    assert _is_question("شرح بده هر کس چیکار می‌کند")
    assert _is_question("کامل بگو")
    assert _is_question("بگو نقش حامد چیست")
    assert not _is_question("سلام")
    assert not _is_question("هی")


def test_single_person_role_is_not_overview() -> None:
    assert not is_board_overview("نقش حامد چیه")
    assert _is_question("نقش حامد چیه")
    assert not is_board_overview("نقشم چیه")
    assert not is_board_overview("نقش من رئیس هیئت مدیره")


def test_tiny_greeting_is_not_overview() -> None:
    assert not is_board_overview("سلام")
    assert not is_board_overview("هی چاربات")
    assert not is_board_overview("درود")


def test_format_board_overview_lists_four_with_mentions_and_notes(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "overview.db")
    store.upsert_user_mapping(
        telegram_user_id=1, member_key="kawe", username="kawe_tg", display_name="Kawe"
    )
    store.upsert_user_mapping(
        telegram_user_id=2,
        member_key="hamed",
        username="alice_tg",
        display_name="Hamed",
    )
    store.upsert_user_mapping(
        telegram_user_id=3, member_key="saman", username="bob_tg", display_name="Saman"
    )
    store.upsert_user_mapping(
        telegram_user_id=4,
        member_key="mohammadreza",
        username="mreza_tg",
        display_name="Mohammadreza",
    )
    store.set_person_fact("kawe", "notes", "work_details", "هماهنگی مشتری و PMO", source="test")
    store.set_person_role("hamed", "مدیرعامل، سرپرست طراحی، طراح", source="test")
    store.set_person_role("saman", "هماهنگ‌کننده عملیات", source="test")

    text = format_board_overview(store)
    assert is_board_overview(KAVEH_SENTENCE)
    assert "@kawe_tg" in text
    assert "@alice_tg" in text
    assert "@bob_tg" in text
    assert "@mreza_tg" in text
    assert "کاوه" in text
    assert "حامد" in text
    assert "سامان" in text
    assert "محمدرضا" in text
    assert "مدیرعامل" in text
    assert "هماهنگ‌کننده عملیات" in text
    assert "هماهنگی مشتری و PMO" in text
    assert "غزل" not in text
