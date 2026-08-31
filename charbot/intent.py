"""Speech-act gate: classify BEFORE create, role dump, or list."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from charbot.glossary import is_learn_utterance
from charbot.members import find_member_in_text
from charbot.voice import is_voice_confirmation


class SpeechActKind(str, Enum):
    LIST_TASKS = "list_tasks"
    QUERY_ROLE = "query_role"
    CREATE_TASK = "create_task"
    REPORT = "report"
    CONFIRM = "confirm"
    ASK_WHICH = "ask_which"
    LEARN = "learn"
    CHECKIN = "checkin"
    UNKNOWN = "unknown"


# Back-compat aliases used by older call sites.
class UtteranceKind(str, Enum):
    QUESTION = "question"
    LIST_MY_WORK = "list_my_work"
    CONFIRM = "confirm"
    NEW_WORK = "new_work"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SpeechAct:
    kind: SpeechActKind
    person_key: str | None = None
    for_speaker: bool = False
    board_open: bool = False


PERSON_CALLBACK_ID = {
    "kawe": 1,
    "hamed": 2,
    "saman": 3,
    "mohammadreza": 4,
    "ghazal": 5,
}
CALLBACK_ID_PERSON = {v: k for k, v in PERSON_CALLBACK_ID.items()}

_EXPLICIT_CREATE = ("ثبت کن", "ثبتش کن", "یه تسک", "یک تسک", "تسک جدید", "کار جدید")
_META_SAVE = ("ذخیره کن", "بعنوان یه کار", "به عنوان یه کار")
_FOLLOWUP = ("متوجه شدی", "فهمیدی", "ذخیره کن", "چی شد")
_INTERRO_RE = re.compile(
    r"(?:^|[\s،,])(?:چی|چیه|چیست|چیان|چه|چرا|کجا|آیا|مگه|چطور|چگونه)(?:[\s؟?]|$)"
)
_KI_Q_RE = re.compile(r"(?:^|[\s،,])کی(?:[\s؟?]|$)")
_MUST_RE = re.compile(r"(?:من\s+)?باید\s+\S+")
# Inventory morphology — «کارهای» is NOT the create prefix «کار».
_INVENTORY_RE = re.compile(
    r"کار(?:ها|های|هام|ام|ای)|"
    r"تسک(?:‌|\s)?ها|"
    r"وظایف|"
    r"لیست\s+کار|"
    r"سپرد(?:ی|ین|ه)"
)
_SELF_RE = re.compile(
    r"کار(?:ها|های|ای)?(?:م)|کارهام|کارام|"
    r"کار(?:ها|های|ای).{0,12}(?:من|خودم)|"
    r"کارای\s+من|"
    r"تسک(?:‌|\s)?های\s+من|لیست\s+کارهام|به\s+من\s+سپرد|چی\s+به\s+من"
)
_BOARD_OPEN_RE = re.compile(
    r"^(?:open tasks?|tasks? open|تسک(?:‌|\s)?های باز|لیست تسک|"
    r"کارهای باز|لیست کارها)[؟?]*$",
    re.IGNORECASE,
)
_CHIKAR_RE = re.compile(r"چی\s*کار(?:ی)?(?:\s+می|\s+می‌|ه)")
_TASK_PREFIX_RE = re.compile(
    r"^(?:(?:task|new task|تسک(?:\s+جدید)?)\s*[:\-]|کار\s*[:\-]|we need to\s+)",
    re.IGNORECASE,
)
_OBJECT_IMPERATIVE_RE = re.compile(
    r"(?:\sرا\s|\sرو\s).{0,80}(?:کن(?:ید|م|یم|ه)?|بفرست|بررسی|بساز|تحویل)"
)


def is_interrogative(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if "؟" in t or "?" in t:
        return True
    if _INTERRO_RE.search(t) or _KI_Q_RE.search(t):
        return True
    return any(w in t for w in ("چی بودن", "چی بود", "چیه", "چیان"))


def has_inventory_ask(text: str) -> bool:
    """Recall of existing work: کارها / تسک‌ها / وظایف / سپرده — not bare «کار»."""
    return bool(_INVENTORY_RE.search(text or ""))


def is_list_my_work(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _SELF_RE.search(t):
        return True
    if "سپردی" in t or "سپردین" in t:
        named = find_member_in_text(t)
        return named is None
    return False


def is_explicit_create(text: str) -> bool:
    t = text or ""
    if any(h in t for h in _EXPLICIT_CREATE):
        return True
    if any(h in t for h in _META_SAVE) and not is_interrogative(t):
        if re.fullmatch(r".{0,12}ذخیره\s+کن.{0,40}", t) and "باید" not in t:
            return False
        return "باید" in t
    return False


def _asks_role(text: str) -> bool:
    t = text or ""
    if has_inventory_ask(t):
        return False
    if any(h in t for h in ("فهمیدی", "گفتم")) and "نقش" in t:
        return False
    if "عنوان شغلی" in t:
        return True
    if re.search(r"(?:^|[\s،])سمت(?:[\s؟]|$)", t):
        return True
    if "چیکاره" in t or "چی کاره" in t:
        return True
    if "نقش" not in t:
        return False
    return is_interrogative(t) or bool(re.search(r"نقش\s+\S+", t))


def _is_new_work(text: str) -> bool:
    t = text or ""
    if has_inventory_ask(t) or _asks_role(t):
        return False
    if is_interrogative(t) and not is_explicit_create(t):
        return False
    if _MUST_RE.search(t):
        return True
    if _OBJECT_IMPERATIVE_RE.search(t):
        return True
    if is_explicit_create(t):
        return True
    if _TASK_PREFIX_RE.search(t.strip()):
        return True
    return False


_CHECKIN_END_RE = re.compile(
    r"(?:اوکی|اکی|okay|ok|باشه|باش)\s*[؟?]\s*$",
    re.IGNORECASE,
)
_CHECKIN_HINTS = ("متوجه شدی", "فهمیدی", "گرفتی؟", "گرفتی ?", "درست متوجه")


def is_checkin(text: str) -> bool:
    """«اوکی؟» / متوجه شدی؟ = did you get it, not a voice-transcript yes."""
    t = (text or "").strip()
    if not t:
        return False
    if _CHECKIN_END_RE.search(t):
        return True
    return any(h in t for h in _CHECKIN_HINTS)


def _is_confirm(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    # Teaching or «اوکی؟» check-in is not a transcript lock.
    if is_learn_utterance(t) or (is_checkin(t) and ("؟" in t or "?" in t) and len(t) > 12):
        return False
    if is_voice_confirmation(t) and len(t) <= 80 and not is_checkin(t):
        return True
    if has_inventory_ask(t) or _asks_role(t) or is_learn_utterance(t):
        return False
    return any(h in t for h in _FOLLOWUP)


def _is_report(text: str) -> bool:
    t = text or ""
    return "گزارش" in t and any(w in t for w in ("هفته", "ماه", "از "))


def _is_ask_which(text: str) -> bool:
    t = text or ""
    if has_inventory_ask(t) or _asks_role(t):
        return False
    return bool(_CHIKAR_RE.search(t))


def classify_speech_act(text: str, *, speaker_key: str | None = None) -> SpeechAct:
    """Single gate used before create / role / list."""
    del speaker_key
    raw = (text or "").strip()
    if not raw:
        return SpeechAct(SpeechActKind.UNKNOWN)

    if has_inventory_ask(raw):
        named = find_member_in_text(raw)
        self = is_list_my_work(raw)
        if self and not named:
            return SpeechAct(SpeechActKind.LIST_TASKS, for_speaker=True)
        if named:
            return SpeechAct(SpeechActKind.LIST_TASKS, person_key=named)
        if _BOARD_OPEN_RE.match(raw) or (
            re.search(r"کارهای\s+باز", raw) and not self
        ):
            return SpeechAct(SpeechActKind.LIST_TASKS, board_open=True)
        return SpeechAct(SpeechActKind.LIST_TASKS, for_speaker=True)

    if _asks_role(raw):
        named = find_member_in_text(raw)
        self_role = any(w in raw for w in ("نقش من", "نقشم"))
        return SpeechAct(
            SpeechActKind.QUERY_ROLE,
            person_key=named,
            for_speaker=self_role and named is None,
        )

    if _is_report(raw):
        return SpeechAct(SpeechActKind.REPORT)

    if is_learn_utterance(raw):
        return SpeechAct(SpeechActKind.LEARN)

    # «اوکی؟» after teaching, or a bare check-in. Pending voice already
    # consumed short yes-words in handle_pending_voice_text.
    if is_checkin(raw):
        return SpeechAct(SpeechActKind.CHECKIN)

    if _is_confirm(raw):
        return SpeechAct(SpeechActKind.CONFIRM)

    if _is_ask_which(raw):
        return SpeechAct(
            SpeechActKind.ASK_WHICH,
            person_key=find_member_in_text(raw),
        )

    if _is_new_work(raw):
        return SpeechAct(SpeechActKind.CREATE_TASK, person_key=find_member_in_text(raw))

    return SpeechAct(SpeechActKind.UNKNOWN)


def classify_utterance(text: str) -> UtteranceKind:
    act = classify_speech_act(text)
    if act.kind == SpeechActKind.LIST_TASKS:
        return UtteranceKind.LIST_MY_WORK
    if act.kind == SpeechActKind.CONFIRM:
        return UtteranceKind.CONFIRM
    if act.kind == SpeechActKind.CREATE_TASK:
        return UtteranceKind.NEW_WORK
    if act.kind in (SpeechActKind.QUERY_ROLE, SpeechActKind.ASK_WHICH):
        return UtteranceKind.QUESTION
    if act.kind == SpeechActKind.UNKNOWN and is_interrogative(text):
        return UtteranceKind.QUESTION
    return UtteranceKind.UNKNOWN


def must_reply(act: SpeechAct, text: str) -> bool:
    """Directed speech in the allowed group is never dropped."""
    if act.kind in (
        SpeechActKind.LIST_TASKS,
        SpeechActKind.QUERY_ROLE,
        SpeechActKind.REPORT,
        SpeechActKind.ASK_WHICH,
        SpeechActKind.LEARN,
        SpeechActKind.CHECKIN,
        SpeechActKind.CREATE_TASK,
    ):
        return True
    t = text or ""
    if is_interrogative(t) or is_checkin(t) or is_learn_utterance(t):
        return True
    return False


def may_create_task(text: str) -> bool:
    """CREATE only for new-work meaning. Questions / recall never insert."""
    return classify_speech_act(text).kind == SpeechActKind.CREATE_TASK


_REMNANT_TITLE_RE = re.compile(
    r"^(?:های|هام|ام|ای)\s+|چیان?\s*$|[؟?]"
)


def is_question_shaped_title(title: str) -> bool:
    """Persistence invariant: leftover inventory questions must never insert."""
    t = (title or "").strip()
    if not t:
        return True
    if _REMNANT_TITLE_RE.search(t):
        return True
    if has_inventory_ask(t) and not is_explicit_create(t):
        return True
    if is_interrogative(t) and not is_explicit_create(t):
        return True
    return False
