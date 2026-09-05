"""Board member definitions and Telegram identity mapping."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BoardMember:
    key: str
    display_name: str
    aliases: tuple[str, ...]


BOARD_MEMBERS: tuple[BoardMember, ...] = (
    BoardMember("kawe", "Kawe", ("kawe", "kaveh", "kave", "کاوه", "کاو")),
    BoardMember("hamed", "Hamed", ("hamed", "حمید", "حامد")),
    BoardMember("saman", "Saman", ("saman", "سامان")),
    BoardMember(
        "mohammadreza",
        "Mohammadreza",
        ("mohammadreza", "mohammad", "mreza", "محمدرضا", "محمد"),
    ),
    BoardMember("ghazal", "Ghazal", ("ghazal", "غزل")),
)

MEMBER_BY_KEY = {m.key: m for m in BOARD_MEMBERS}


def resolve_member_name(text: str) -> str | None:
    """Return member key if text matches a board member name or alias."""
    normalized = text.strip().lower()
    if not normalized:
        return None
    for member in BOARD_MEMBERS:
        if normalized == member.key or normalized == member.display_name.lower():
            return member.key
        for alias in member.aliases:
            if normalized == alias.lower():
                return member.key
    return None


def member_display(key: str | None) -> str:
    if not key:
        return "—"
    member = MEMBER_BY_KEY.get(key)
    return member.display_name if member else key


def find_member_in_text(text: str) -> str | None:
    """Return member key if any alias appears inside text (not only exact match)."""
    hay = text.strip().lower()
    if not hay:
        return None
    best: str | None = None
    best_len = 0
    for member in BOARD_MEMBERS:
        for name in (member.key, member.display_name, *member.aliases):
            n = name.lower()
            if n and n in hay and len(n) >= best_len:
                best = member.key
                best_len = len(n)
    return best

FA_DISPLAY = {
    "kawe": "کاوه",
    "hamed": "حامد",
    "saman": "سامان",
    "mohammadreza": "محمدرضا",
    "ghazal": "غزل",
}


def member_display_fa(key: str | None) -> str:
    if not key:
        return "نامشخص"
    return FA_DISPLAY.get(key, member_display(key))


# People who are not in the primary Telegram group: tasks keep their assignee,
# but follow-up questions and @mentions go through another member.
FOLLOWUP_VIA: dict[str, str] = {
    "ghazal": "hamed",
}


def chase_via(assignee_key: str | None) -> str | None:
    """Who to ping for a follow-up. Assignee on the task does not change."""
    if not assignee_key:
        return None
    return FOLLOWUP_VIA.get(assignee_key, assignee_key)


def followup_addressee_fa(assignee_key: str | None) -> str | None:
    """Persian name used to open an active-card question.

    For Ghazal (not in the group): «حامد (برای غزل)» so Hamed is asked and
    the real owner stays visible.
    """
    if not assignee_key:
        return None
    via = chase_via(assignee_key)
    if via is None:
        return None
    if via != assignee_key:
        return f"{member_display_fa(via)} (برای {member_display_fa(assignee_key)})"
    return member_display_fa(via)
