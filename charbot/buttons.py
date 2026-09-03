"""Context-bound Telegram answer chips — labels follow the question, not a global menu."""

from __future__ import annotations

import re
from collections.abc import Iterable

from charbot.formatting import to_fa_digits

# callback_data: kind:choice:id  (Telegram limit 64 bytes)
_CHOICE_RE = re.compile(r"^[a-z]{1,12}$")
_KINDS = {"vc", "td", "fu", "ask", "rp", "qa"}

# Voice confirm: colleague-tone defaults. Extra chips may be added from the transcript.
VC_OK_FA = "همین بود"
VC_EDIT_FA = "این را اصلاح می‌کنم"

_TRIP_PLACES = (
    ("مشهد", "مشهد"),
    ("تهران", "تهران"),
    ("اصفهان", "اصفهان"),
    ("شیراز", "شیراز"),
    ("تبریز", "تبریز"),
    ("کیش", "کیش"),
)
_PAY_HINTS = ("واریز", "پرداخت", "پول", "فیش", "حساب", "هزینه", "فاکتور")
_MEET_HINTS = ("جلسه", "میтинг", "میتینگ", "قرار")
_SEND_HINTS = ("بفرست", "ارسال", "لینک", "فایل", "لوگو", "نمونه")


def parse_callback_data(data: str) -> tuple[str, str, str] | None:
    """Return (kind, choice, id) or None. Unknown taps fail safe."""
    parts = (data or "").strip().split(":")
    if len(parts) != 3:
        return None
    kind, choice, payload = parts
    if kind not in _KINDS or not _CHOICE_RE.match(choice):
        return None
    if not payload.lstrip("-").isdigit():
        return None
    return kind, choice, payload


def _clip(label: str, limit: int = 32) -> str:
    text = re.sub(r"\s+", " ", (label or "").strip())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _hay(question: str, context: str) -> str:
    return f"{question or ''}\n{context or ''}"


def _place_in(hay: str) -> str | None:
    for needle, place in _TRIP_PLACES:
        if needle in hay:
            return place
    if "سفر" in hay or "پرواز" in hay or "قطار" in hay:
        return "سفر"
    return None


def _chips_for(question: str, kind: str, context: str) -> list[tuple[str, str]]:
    """Natural answers to THIS question. (label, choice-token)."""
    hay = _hay(question, context)
    kind = (kind or "").strip().lower()

    if kind == "qa" or ("کارهاش" in hay and "نقشش" in hay):
        return [("کارهاش", "tasks"), ("نقشش", "role")]

    if kind == "rp" or (kind != "vc" and "گزارش" in hay):
        return [("این هفته", "week"), ("این ماه", "month")]

    if kind == "vc":
        chips: list[tuple[str, str]] = [(VC_OK_FA, "ok"), (VC_EDIT_FA, "edit")]
        # Optional extra only when the spoken text itself is a clear yes/no fact.
        place = _place_in(context or "")
        if place and place != "سفر":
            chips.append((f"راجع به {place} بود", "ok"))
        elif any(h in (context or "") for h in _PAY_HINTS):
            chips.append(("راجع به پرداخت بود", "ok"))
        return _dedupe(chips)[:4]

    place = _place_in(hay)
    if place:
        went = f"رفتم {place}" if place != "سفر" else "رفتم"
        stayed = "نرفتم"
        return _dedupe([(went, "go"), (stayed, "nogo"), ("عوض شد", "changed")])[:4]

    if any(h in hay for h in _PAY_HINTS):
        return [
            ("واریز شد", "paid"),
            ("هنوز نه", "not"),
            ("فردا پیگیری می‌کنم", "later"),
        ]

    if any(h in hay for h in _MEET_HINTS):
        return [("برگزار شد", "done"), ("نشد", "not"), ("عوض شد", "changed")]

    if any(h in hay for h in _SEND_HINTS):
        return [("فرستادم", "done"), ("هنوز نه", "not"), ("فردا می‌فرستم", "later")]

    if kind in {"td", "fu"}:
        return [
            ("انجام شد", "done"),
            ("هنوز نه", "not"),
            ("فردا پیگیری می‌کنم", "later"),
        ]

    if kind == "ask":
        if "موعد" in hay and "مسئول" not in hay:
            return [("امروز", "today"), ("فردا", "tomorrow"), ("هفته بعد", "week")]
        return []

    return []


def _dedupe(chips: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for label, choice in chips:
        key = label.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append((_clip(key), choice))
    return out


def question_buttons(
    question_text: str,
    *,
    kind: str,
    context: str = "",
    target_id: int | str = 0,
) -> list[list[tuple[str, str]]]:
    """2–4 inline chips bound to this question. Each cell is (label, callback_data)."""
    chips = _chips_for(question_text, kind, context)
    if not chips:
        return []
    tid = str(target_id)
    kind = (kind or "fu").strip().lower() or "fu"
    pairs = [(label, f"{kind}:{choice}:{tid}") for label, choice in chips[:4]]
    if len(pairs) <= 2:
        return [pairs]
    if len(pairs) == 3:
        return [pairs]
    return [pairs[:2], pairs[2:]]


def task_pick_buttons(items: list[tuple[str, int]]) -> list[list[tuple[str, str]]]:
    """2-4 title chips; the buttons ARE the list, so each carries its task
    number (no visible "#12" anchor elsewhere, but here a human genuinely
    needs the reference to tell same-sounding tasks apart). Tap marks that
    task done (td:done:id)."""
    pairs = []
    for label, tid in items[:4]:
        title = (label or "").strip()
        if not title:
            continue
        tagged = f"کار {to_fa_digits(tid)}: {title}"
        pairs.append((_clip(tagged), f"td:done:{tid}"))
    if not pairs:
        return []
    if len(pairs) <= 3:
        return [pairs]
    return [pairs[:2], pairs[2:]]


def followup_question(title: str, owner_fa: str | None = None) -> str:
    """One short colleague-style ask about this piece of work."""
    work = (title or "کار").strip() or "کار"
    who = (owner_fa or "").strip()
    prefix = f"{who}، " if who else ""
    place = _place_in(work)
    if place and place != "سفر":
        return f"{prefix}{place} چه شد؟ رفتی؟"
    if any(h in work for h in _PAY_HINTS):
        return f"{prefix}واریز {work} انجام شد؟"
    return f"{prefix}{work} چه شد؟"


def choice_means_done(choice: str) -> bool:
    return choice in {"done", "paid", "go", "yes", "ok"}


def choice_means_wait(choice: str) -> bool:
    return choice in {"not", "nogo", "later"}


def choice_means_changed(choice: str) -> bool:
    return choice in {"changed"}
