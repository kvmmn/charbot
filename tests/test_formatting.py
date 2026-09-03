from __future__ import annotations

from datetime import UTC, date, datetime

from charbot.formatting import (
    DIGEST_INLINE_MAX,
    LIST_INLINE_MAX,
    PERSON_MARK,
    format_active_card,
    format_daily_plan,
    format_overdue_alert,
    format_resolved,
    format_task,
    format_task_confirmation,
    format_task_digest,
    format_task_list,
    format_task_question,
    jalali_label,
    to_fa_digits,
)
from charbot.store import Task, TaskStatus

TODAY = date(2026, 9, 3)


def _task(**kw):
    now = datetime.now(UTC)
    base = dict(
        id=3,
        group_id=-1,
        title="اتاق سیگار مشهد",
        description="با سرپرست کارگاه مقیم؛ جزئیات طولانی که نباید در لیست بیاید.",
        assignee_key="saman",
        due_date=None,
        status=TaskStatus.OPEN,
        created_by_user_id=None,
        created_at=now,
        updated_at=now,
        completed_at=None,
    )
    base.update(kw)
    return Task(**base)


# ---------------------------------------------------------------------------
# Persian digits + Jalali calendar
# ---------------------------------------------------------------------------


def test_to_fa_digits_translates_ascii():
    assert to_fa_digits(1405) == "۱۴۰۵"
    assert to_fa_digits("29/8") == "۲۹/۸"


def test_jalali_label_known_dates():
    # Real overdue set from the incident: 29/30 Aug + 1 Sep 2026.
    assert jalali_label(date(2026, 8, 29)) == "۷ شهریور"
    assert jalali_label(date(2026, 8, 30)) == "۸ شهریور"
    assert jalali_label(date(2026, 9, 1)) == "۱۰ شهریور"


# ---------------------------------------------------------------------------
# Single task card / confirmation — no blockquote, natural sentence, no "·"
# ---------------------------------------------------------------------------


def test_task_card_has_no_blockquote_and_no_middle_dot():
    t = _task(due_date=date(2026, 8, 29), assignee_key="ghazal")
    text = format_task(t, today=TODAY)
    assert "<blockquote" not in text
    assert "·" not in text
    assert "#" not in text  # no bare id anchor
    assert "🟣 غزل" in text
    assert "۷ شهریور" in text
    assert "۵ روز عقب‌افتاده" in text


def test_task_list_shows_title_owner_due_no_description():
    t = _task()
    text = format_task_list([t], header="کارهای باز", today=TODAY)
    assert "اتاق سیگار مشهد" in text
    assert "سامان" in text
    assert "بدون موعد" in text
    assert "سرپرست کارگاه" not in text
    assert "description" not in text
    assert "Owner:" not in text


def test_task_list_never_carries_html_keyboard_markup_hint():
    # A read-only list must never itself construct a reply_markup; formatting
    # only ever returns text. This is a structural guard: nothing in the
    # output should look like button/callback wiring.
    tasks = [_task(id=i, due_date=date(2026, 9, 1)) for i in range(1, 3)]
    text = format_task_list(tasks, header="کارهای باز", today=TODAY)
    assert "callback" not in text
    assert "InlineKeyboard" not in text


def test_due_date_uses_jalali_not_gregorian_slash():
    due = date(2026, 8, 30)
    t = _task(due_date=due, assignee_key="hamed")
    text = format_task(t, today=TODAY)
    assert f"{due.day}/{due.month}" not in text
    assert "۸ شهریور" in text


def test_person_rings_are_unique_and_touch_the_name():
    saman = _task(assignee_key="saman", due_date=date(2026, 9, 7))
    hamed = _task(assignee_key="hamed", due_date=date(2026, 9, 8), title="لوگو شی")
    text_s = format_task(saman, today=TODAY)
    text_h = format_task(hamed, today=TODAY)
    assert "🟠 سامان" in text_s
    assert "🟢 حامد" in text_h
    assert "🟠" not in text_h
    assert len(set(PERSON_MARK.values())) == len(PERSON_MARK)


def test_task_confirmation_has_bold_type_line_then_blank_then_payload():
    t = _task(due_date=None)
    text = format_task_confirmation(t)
    lines = text.split("\n")
    assert lines[0] == "<b>ثبت شد</b>"
    assert lines[1] == ""
    assert "اتاق سیگار مشهد" in text


def test_task_confirmation_note_appears_between_header_and_card():
    t = _task()
    text = format_task_confirmation(t, note="مسئول: سامان")
    assert text.index("مسئول: سامان") < text.index("اتاق سیگار مشهد")


# ---------------------------------------------------------------------------
# Lists: read-only, numbered, expandable past the threshold
# ---------------------------------------------------------------------------


def test_empty_list_is_read_only_and_named():
    text = format_task_list([], header="کارهای باز")
    assert text.startswith("<b>کارهای باز</b>")
    assert "چیزی" in text


def test_list_numbers_use_persian_digits():
    tasks = [_task(id=i, title=f"کار {i}", due_date=None) for i in range(1, 4)]
    text = format_task_list(tasks, header="کارهای باز", today=TODAY)
    assert "۱." in text
    assert "۲." in text
    assert "۳." in text
    assert "1." not in text


def test_list_over_threshold_uses_expandable_blockquote():
    tasks = [
        _task(id=i, title=f"کار شماره {i}", due_date=date(2026, 9, 1))
        for i in range(1, LIST_INLINE_MAX + 4)
    ]
    text = format_task_list(tasks, header="کارهای باز", today=TODAY)
    assert "<blockquote expandable>" in text
    # the earliest (highest-priority-by-order) items stay inline
    assert text.index("کار شماره 1") < text.index("<blockquote expandable>")
    # the overflow items are inside the expandable tail
    tail = text.split("<blockquote expandable>", 1)[1]
    assert f"کار شماره {LIST_INLINE_MAX + 3}" in tail


def test_short_list_has_no_expandable_section():
    tasks = [_task(id=i, due_date=date(2026, 9, 1)) for i in range(1, 4)]
    text = format_task_list(tasks, header="کارهای باز", today=TODAY)
    assert "expandable" not in text


# ---------------------------------------------------------------------------
# Daily plan
# ---------------------------------------------------------------------------


def test_daily_plan_header_and_counts_no_keyboard_hint():
    tasks = [
        _task(id=1, title="کار یک", assignee_key="saman", due_date=date(2026, 8, 29)),
        _task(id=2, title="کار دو", assignee_key="hamed", due_date=date(2026, 9, 10)),
        _task(id=3, title="کار سه", assignee_key=None, due_date=None),
    ]
    text = format_daily_plan(tasks, today=TODAY)
    assert text.startswith("<b>برنامهٔ امروز</b>")
    assert "۳ کار" in text
    assert "۲ تصمیم" in text  # one overdue + one unassigned
    assert "کار یک" in text and "کار دو" in text and "کار سه" in text


def test_daily_plan_empty_is_read_only():
    text = format_daily_plan([])
    assert text.startswith("<b>برنامهٔ امروز</b>")


# ---------------------------------------------------------------------------
# Grouped digest: person-first, most-urgent-first, ring+name once
# ---------------------------------------------------------------------------


def _real_overdue_set() -> list[Task]:
    return [
        _task(id=101, title="اجرای سه لوگو", assignee_key="ghazal", due_date=date(2026, 8, 29)),
        _task(
            id=102,
            title="صورتجلسه هیئت مدیره",
            assignee_key="hamed",
            due_date=date(2026, 8, 30),
        ),
        _task(
            id=103,
            title="قیمت فیلم‌بردار اینستاگرام",
            assignee_key="ghazal",
            due_date=date(2026, 9, 1),
        ),
        _task(id=104, title="جلسه سه‌شنبه", assignee_key="mohammadreza", due_date=date(2026, 9, 1)),
        _task(id=105, title="بلیط پرواز مشهد", assignee_key="saman", due_date=date(2026, 9, 1)),
    ]


def test_digest_groups_each_task_under_its_owner():
    tasks = _real_overdue_set()
    text = format_task_digest(tasks, header="کارهای عقب‌افتاده", today=TODAY)
    assert text.startswith("<b>کارهای عقب‌افتاده</b>")
    assert "۵ کار برای ۴ نفر" in text
    ghazal_block = text.split("🟣 غزل")[1].split("<b>")[0]
    assert "اجرای سه لوگو" in ghazal_block
    assert "قیمت فیلم‌بردار اینستاگرام" in ghazal_block
    assert "صورتجلسه هیئت مدیره" not in ghazal_block


def test_digest_ring_and_name_appear_once_in_section_header_only():
    tasks = _real_overdue_set()
    text = format_task_digest(tasks, header="کارهای عقب‌افتاده", today=TODAY)
    # Ghazal's ring+name appears exactly once (the section header), not once
    # per item under it.
    assert text.count("🟣 غزل") == 1


def test_digest_orders_most_overdue_person_first():
    tasks = _real_overdue_set()
    text = format_task_digest(tasks, header="کارهای عقب‌افتاده", today=TODAY)
    # Ghazal owns the oldest overdue item (29 Aug) so her section leads.
    assert text.index("غزل") < text.index("حامد")


def test_digest_unassigned_goes_last_on_priority_tie():
    tasks = [
        _task(id=1, title="کار مسئول‌دار", assignee_key="saman", due_date=date(2026, 9, 1)),
        _task(id=2, title="کار بدون مسئول", assignee_key=None, due_date=date(2026, 9, 1)),
    ]
    text = format_task_digest(tasks, header="کارهای عقب‌افتاده", today=TODAY)
    assert text.index("🟠 سامان") < text.index("⚪ بدون مسئول")


def test_digest_expandable_past_threshold_keeps_urgent_items_visible():
    urgent = [
        _task(id=i, title=f"فوری {i}", assignee_key="ghazal", due_date=date(2026, 8, 20 + i))
        for i in range(1, DIGEST_INLINE_MAX + 1)
    ]
    low_priority = _task(
        id=999, title="کار کم‌فوریت", assignee_key="saman", due_date=date(2026, 9, 20)
    )
    text = format_task_digest(urgent + [low_priority], header="کارهای عقب‌افتاده", today=TODAY)
    assert "<blockquote expandable>" in text
    head, tail = text.split("<blockquote expandable>", 1)
    assert "فوری 1" in head
    assert "کار کم‌فوریت" in tail


def test_digest_empty_is_read_only():
    text = format_task_digest([], header="کارهای عقب‌افتاده")
    assert text.startswith("<b>کارهای عقب‌افتاده</b>")


# ---------------------------------------------------------------------------
# Active card / resolved edit / alert
# ---------------------------------------------------------------------------


def test_active_card_shape():
    text = format_active_card("غزل، سه اجرای لوگو را فرستادی؟", "موعد ۷ شهریور، ۵ روز عقب‌افتاده")
    lines = text.split("\n")
    assert lines[0] == "<b>پاسخ لازم</b>"
    assert lines[1] == ""
    assert lines[2] == "غزل، سه اجرای لوگو را فرستادی؟"
    assert lines[3] == "موعد ۷ شهریور، ۵ روز عقب‌افتاده"


def test_task_question_includes_due_metadata():
    t = _task(assignee_key="ghazal", due_date=date(2026, 8, 29))
    text = format_task_question(t, "غزل، این کار چه شد؟", today=TODAY)
    assert text.startswith("<b>پاسخ لازم</b>")
    assert "۵ روز عقب‌افتاده" in text


def test_task_question_flags_unassigned():
    t = _task(assignee_key=None, due_date=date(2026, 8, 29))
    text = format_task_question(t, "این کار چه شد؟", today=TODAY)
    assert "بدون مسئول" in text


def test_resolved_edit_shows_who_and_choice_no_keyboard_hint():
    when = datetime(2026, 9, 3, 17, 20)
    text = format_resolved("غزل", "تا فردا می‌فرستم", when=when)
    assert text.startswith("<b>ثبت شد</b>")
    assert "غزل: تا فردا می‌فرستم" in text
    assert "۱۷:۲۰" in text
    assert "امروز" in text


def test_overdue_alert_shape():
    t = _task(assignee_key="ghazal", due_date=date(2026, 8, 29), title="اجرای سه لوگو")
    text = format_overdue_alert(
        t, consequence="تحویل جمعه عقب می‌افتد", next_action="همین امروز بفرست", today=TODAY
    )
    assert text.startswith("<b>عقب‌افتاده</b>")
    assert "🟣 غزل" in text
    assert "اجرای سه لوگو" in text
    assert "تحویل جمعه عقب می‌افتد" in text
    assert "همین امروز بفرست" in text
