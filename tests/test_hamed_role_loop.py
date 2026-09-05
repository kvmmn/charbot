"""Regression: Hamed answered his role; the bot must never keep asking him."""

from __future__ import annotations

from pathlib import Path

from charbot.bot import _looks_like_work, _missing_roles, _role_saved
from charbot.store import TaskStore

HAMED_ROLE = "مدیرعامل\nسرپرست طراحی\nطراح"
HAMED_FOLLOWUP = "الان نقشم و گفتم فهمیدی @YourBot"
HAMED_JOKE = "فک میکردم  عاقلی"


def _store_with_hamed(tmp_path: Path) -> TaskStore:
    store = TaskStore(tmp_path / "hamed.db")
    store.upsert_user_mapping(
        telegram_user_id=84184761,
        member_key="hamed",
        username="alice_tg",
        display_name="Hamed",
    )
    store.set_person_role("hamed", "مدیرعامل، سرپرست طراحی، طراح", source="hamed")
    store.set_kv("dialog", "work")
    return store


def test_hamed_role_list_is_not_work() -> None:
    assert not _looks_like_work(HAMED_ROLE)
    assert "مدیرعامل" in HAMED_ROLE


def test_after_hamed_answers_he_is_not_missing(tmp_path: Path) -> None:
    store = _store_with_hamed(tmp_path)
    assert _role_saved(store, "hamed")
    missing = _missing_roles(store)
    assert "hamed" not in missing
    assert missing == ["mohammadreza", "saman"]


def test_hamed_followup_does_not_put_him_back_on_the_list(tmp_path: Path) -> None:
    store = _store_with_hamed(tmp_path)
    raw = HAMED_FOLLOWUP
    already = _role_saved(store, "hamed")
    assert already
    assert any(w in raw for w in ("فهمیدی", "گفتم", "نقشم"))
    assert "hamed" not in _missing_roles(store)


def test_unmatched_joke_does_not_mark_hamed_missing(tmp_path: Path) -> None:
    store = _store_with_hamed(tmp_path)
    assert not _looks_like_work(HAMED_JOKE)
    assert "hamed" not in _missing_roles(store)
