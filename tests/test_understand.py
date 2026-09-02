"""Meaning-based understanding: colloquial Persian, not keyword-only."""

from __future__ import annotations

from datetime import date

from charbot.understand import clean_work_text, extract_task

TODAY = date(2026, 8, 31)
DUE = date(2026, 9, 2)
WORK = (
    "من باید فایل تدوین‌شده قرارداد متعلق به حامد رو بررسی کنم "
    "و اگر نیاز به اصلاح داشت به حامد بگم تا دو روز دیگه"
)
SAVE = "بعنوان یه کار قابل پیگیری ذخیره کن"
SAVE_LONG = "بعنوان یه کار قابل پیگیری ذخیره کن. جزییاتش رو بگو بهم (مسئول، زمان، موضوع)"


def test_extract_contract_review_speaker_not_hamed() -> None:
    r = extract_task(WORK, speaker_key="kawe", today=TODAY)
    assert r.title == "بررسی فایل تدوین‌شده قرارداد حامد"
    assert not (r.title or "").startswith("من باید")
    assert "من باید" not in (r.title or "")
    assert r.assignee_key == "kawe"
    assert r.assignee_key != "hamed"
    assert r.due_date == DUE
    assert r.description is not None
    assert "اگر نیاز به اصلاح داشت به حامد بگو" in r.description
    assert r.confidence == "high"
    assert r.ask is None


def test_save_phrase_uses_context_not_save_kon() -> None:
    cleaned = clean_work_text(SAVE)
    assert "ذخیره" not in cleaned
    assert "بعنوان" not in cleaned
    assert "من باید" not in cleaned
    bare = extract_task(SAVE, speaker_key="kawe", today=TODAY)
    assert not (bare.title or "")
    assert "ذخیره" not in (bare.title or "")
    long_bare = extract_task(SAVE_LONG, speaker_key="kawe", today=TODAY)
    assert "ذخیره" not in (long_bare.title or "")
    r = extract_task(SAVE, speaker_key="kawe", today=TODAY, context=WORK)
    assert r.title == "بررسی فایل تدوین‌شده قرارداد حامد"
    assert r.assignee_key == "kawe"
    assert r.due_date == DUE
    assert "ذخیره" not in (r.title or "")


def test_ask_when_owner_and_due_missing() -> None:
    both = extract_task(
        "فایل تدوین‌شده قرارداد باید بررسی شود",
        speaker_key=None,
        today=TODAY,
    )
    assert both.confidence == "low"
    assert both.ask == "مسئول کیست و موعد کی است؟"
    missing_due = extract_task(
        "من باید فایل قرارداد رو بررسی کنم",
        speaker_key="kawe",
        today=TODAY,
    )
    assert missing_due.assignee_key == "kawe"
    assert missing_due.due_date is None
    assert missing_due.confidence == "low"
    assert missing_due.ask is not None
    assert "موعد" in missing_due.ask
    missing_owner = extract_task(
        "فایل قرارداد رو بررسی کنید تا دو روز دیگه",
        speaker_key=None,
        today=TODAY,
    )
    assert missing_owner.due_date == DUE
    assert missing_owner.assignee_key is None
    assert missing_owner.confidence == "low"
    assert missing_owner.ask is not None
    assert "مسئول" in missing_owner.ask


def test_clean_work_text_strips_fillers_and_zw() -> None:
    messy = "من باید  که  که  فایل\u200b را بررسی کنم ذخیره کن متوجه شدی"
    cleaned = clean_work_text(messy)
    assert "من باید" not in cleaned
    assert "ذخیره کن" not in cleaned
    assert "متوجه شدی" not in cleaned
    assert "\u200b" not in cleaned
    assert "  " not in cleaned


def test_sent_for_is_notify_not_assignee() -> None:
    from charbot.understand import _explicit_assignee, _mask_non_owner_mentions

    masked = _mask_non_owner_mentions("فرستادم برای حامد فایل قرارداد")
    assert "حامد" not in masked
    assert _explicit_assignee("فرستادم برای حامد") is None
