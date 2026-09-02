"""Meaning-based Persian task understanding (not keyword-only)."""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from charbot.members import MEMBER_BY_KEY, find_member_in_text

logger = logging.getLogger(__name__)

# Keep ZWNJ (U+200C): Persian half-space in words like تدوین‌شده.
_ZW_STRIP = dict.fromkeys(map(ord, "\u200b\u200d\u2060\ufeff\u00ad"))

_FILLER_PHRASES = (
    r"به?\s*عنوان\s+(?:یه|یک|ى)\s+کار\s+قابل\s+پیگیری",
    r"بعنوان\s+(?:یه|یک|ى)\s+کار\s+قابل\s+پیگیری",
    r"جزییات(?:ش|ش را|ش رو)\s+بگو(?:\s+بهم)?",
    r"جزئیات(?:ش|ش را|ش رو)\s+بگو(?:\s+بهم)?",
    r"\(\s*مسئول\s*[،,]\s*زمان\s*[،,]\s*موضوع\s*\)",
    r"من\s+باید",
    r"ذخیره\s+کن(?:ید)?",
    r"متوجه\s+شدی",
    r"فهمیدی",
)
_FILLER_RE = re.compile("|".join(_FILLER_PHRASES))
_META_HINTS = (
    "ذخیره کن",
    "متوجه شدی",
    "فهمیدی",
    "بعنوان یه کار قابل پیگیری",
    "به عنوان یه کار قابل پیگیری",
    "چی شد",
)
_WORK_MARKERS = (
    "باید",
    "بررسی",
    "بفرست",
    "ارسال",
    "تحویل",
    "پیگیری",
    "انجام",
    "آماده",
    "بساز",
    "درست کن",
    "چک کن",
    "بنویس",
)
_ROLE_HINTS = ("نقش من", "نقشم", "نقش تو", "نقش ")
_TASK_PREFIX = re.compile(
    r"^(?:task|new task|تسک(?:\s+جدید)?|کار)\s*[:\-]\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_DUE_IN_TEXT = re.compile(
    r"تا\s+(\d+|یک|دو|سه|چهار|پنج|شش|هفت|هشت|نه|ده)\s+روز(?:\s*(?:دیگه|دیکه|دیگر))?"
)
_FA_NUM = {
    "یک": 1,
    "دو": 2,
    "سه": 3,
    "چهار": 4,
    "پنج": 5,
    "شش": 6,
    "هفت": 7,
    "هشت": 8,
    "نه": 9,
    "ده": 10,
}
_MENTION_RE = re.compile(r"@\S+")
_OBJ_RO_VERB = re.compile(
    r"^(.+?)\s+رو\s+(\S+?)(?:\s+کنم|\s+کنیم|\s+کنه|\s+کنید|\s+کن)?$"
)
_OBJ_RA_VERB = re.compile(
    r"^(.+?)\s+را\s+(\S+?)(?:\s+کنم|\s+کنیم|\s+کنه|\s+کنید|\s+کن)?$"
)
_BAYAD_SHAVAD = re.compile(r"^(.+?)\s+باید\s+(\S+?)(?:\s+شود|\s+بشه|\s+بشود)?$")

ASK_BOTH = "مسئول کیست و موعد کی است؟"
ASK_OWNER = "مسئول کیست؟"
ASK_DUE = "موعد کی است؟"


@dataclass
class UnderstandResult:
    title: str | None = None
    description: str | None = None
    assignee_key: str | None = None
    due_date: date | None = None
    confidence: str = "low"  # 'high' | 'low'
    ask: str | None = None


def clean_work_text(text: str) -> str:
    """Strip colloquial fillers, extra «که», doubled spaces, and zero-width junk."""
    t = (text or "").translate(_ZW_STRIP)
    t = t.replace("\u00a0", " ")
    t = _MENTION_RE.sub(" ", t)
    t = t.replace("چاربات", " ")
    t = _FILLER_RE.sub(" ", t)
    t = re.sub(r"[؟?]+", " ", t)
    t = re.sub(r"(?:^|\s)که\s+که(?:\s|$)", " ", t)
    t = re.sub(r"^\s*که\s+", "", t)
    t = re.sub(r"\s+که\s*$", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip(" .،,;؛-")


def extract_task(
    text: str,
    *,
    speaker_key: str | None = None,
    today: date | None = None,
    context: str | None = None,
) -> UnderstandResult:
    """Extract a precise task; ask a short Persian question instead of guessing."""
    today = today or date.today()
    raw = (text or "").strip()
    if context and _is_meta_followup(raw) and not _looks_like_task_payload(raw):
        raw = context.strip()
    if not raw:
        return UnderstandResult(confidence="low")
    from charbot.intent import SpeechActKind, classify_speech_act, is_completion_report

    act = classify_speech_act(raw, speaker_key=speaker_key)
    if act.kind in (
        SpeechActKind.LIST_TASKS,
        SpeechActKind.QUERY_ROLE,
        SpeechActKind.ASK_WHICH,
        SpeechActKind.REPORT,
        SpeechActKind.REPORT_DONE,
    ) or is_completion_report(raw):
        return UnderstandResult(confidence="low")
    if _is_role_chatter(raw) and not _looks_like_task_payload(raw):
        return UnderstandResult(confidence="low")

    heuristic = _heuristic_extract(raw, speaker_key=speaker_key, today=today)
    llm = _try_llm_extract(raw, speaker_key=speaker_key, today=today)
    merged = _merge_llm(heuristic, llm, today)
    return _finalize(merged, raw, speaker_key)


def _is_meta_followup(text: str) -> bool:
    return any(h in (text or "") for h in _META_HINTS)


def _looks_like_task_payload(text: str) -> bool:
    cleaned = clean_work_text(text)
    if len(cleaned) < 3:
        return False
    if any(m in cleaned for m in _WORK_MARKERS):
        return True
    if _TASK_PREFIX.match(cleaned):
        return True
    return bool(re.search(r"\sرو\s+\S+", cleaned) or re.search(r"\sرا\s+\S+", cleaned))


def _is_role_chatter(text: str) -> bool:
    return any(h in text for h in _ROLE_HINTS) and "باید" not in text


def _strip_mentions(text: str) -> str:
    t = _MENTION_RE.sub(" ", text)
    t = t.replace("چاربات", " ")
    return re.sub(r"\s+", " ", t).strip()


def _parse_due_in_text(text: str, today: date) -> date | None:
    from charbot.nlp import parse_date, parse_due_in_text

    d = parse_due_in_text(text, today=today)
    if d:
        return d
    t = _strip_mentions(text)
    m = _DUE_IN_TEXT.search(t)
    if m:
        raw_n = m.group(1)
        n = int(raw_n) if raw_n.isdigit() else _FA_NUM.get(raw_n)
        if n is not None:
            return today + timedelta(days=n)
    if re.search(r"تا\s+فردا", t):
        return today + timedelta(days=1)
    if re.search(r"تا\s+امروز", t):
        return today
    return parse_date(t, today=today)


def _strip_due_phrases(text: str) -> str:
    t = _DUE_IN_TEXT.sub(" ", text)
    t = re.sub(r"تا\s+فردا", " ", t)
    t = re.sub(r"تا\s+امروز", " ", t)
    t = re.sub(r"تا\s+هفته(?:ٔ|‌)?\s*بعد", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _is_first_person(text: str) -> bool:
    t = _strip_mentions(text)
    if re.search(r"من\s+باید", t):
        return True
    if re.search(r"باید\s+.+\s+کن(?:م|یم)\b", t):
        return True
    if re.search(r"کنم\b", t) and "باید" in t:
        return True
    return False


def _mask_non_owner_mentions(text: str) -> str:
    """Blank people named as file/contract owner or notify-target, not assignee."""
    t = text
    names: list[str] = []
    for member in MEMBER_BY_KEY.values():
        names.extend([member.key, member.display_name, *member.aliases])
    names = sorted({n for n in names if n}, key=len, reverse=True)
    for name in names:
        n = re.escape(name)
        t = re.sub(rf"متعلق\s+به\s+{n}", " ", t, flags=re.IGNORECASE)
        t = re.sub(rf"مربوط\s+به\s+{n}", " ", t, flags=re.IGNORECASE)
        t = re.sub(rf"مال\s+{n}", " ", t, flags=re.IGNORECASE)
        t = re.sub(
            rf"به\s+{n}\s+(?:بگو|بگم|بگیم|بگه|اطلاع|خبر|فرستاد)",
            " ",
            t,
            flags=re.IGNORECASE,
        )
        t = re.sub(
            rf"فرستاد(?:م|یم|ه)?\s+برای\s+{n}",
            " ",
            t,
            flags=re.IGNORECASE,
        )
        t = re.sub(
            rf"برای\s+{n}\s+فرستاد",
            " ",
            t,
            flags=re.IGNORECASE,
        )
    return re.sub(r"\s+", " ", t).strip()


def _explicit_assignee(text: str) -> str | None:
    masked = _mask_non_owner_mentions(text)
    patterns = (
        r"مسئول(?:ش|یت)?\s+(?:با\s+)?(\S+)",
        r"بسپار\s+به\s+(\S+)",
        r"واگذار\s+به\s+(\S+)",
        r"برای\s+(\S+)\s+بگذار",
    )
    for pat in patterns:
        m = re.search(pat, masked)
        if not m:
            continue
        found = find_member_in_text(m.group(1))
        if found:
            return found
    m = re.search(r"^(\S+)\s+باید\s+", masked)
    if m:
        found = find_member_in_text(m.group(1))
        if found:
            return found
    return find_member_in_text(masked)


def _title_from_main(main: str) -> str:
    main = clean_work_text(main)
    main = re.sub(r"متعلق\s+به\s+", "", main)
    main = main.strip(" .،,")
    for cre in (_OBJ_RO_VERB, _OBJ_RA_VERB):
        m = cre.match(main)
        if m:
            obj = re.sub(r"متعلق\s+به\s+", "", m.group(1).strip())
            obj = re.sub(r"\s+", " ", obj).strip()
            verb = m.group(2).strip()
            return clean_work_text(f"{verb} {obj}")
    m = _BAYAD_SHAVAD.match(main)
    if m:
        obj = re.sub(r"متعلق\s+به\s+", "", m.group(1).strip())
        verb = m.group(2).strip()
        return clean_work_text(f"{verb} {obj}")
    title = re.sub(r"\s+کنم$|\s+کنیم$|\s+کنه$|\s+کنید$|\s+شود$|\s+بشه$", "", main)
    title = re.sub(r"^باید\s+", "", title.strip())
    return clean_work_text(title)


def _normalize_desc(text: str) -> str:
    d = text.replace("بگم", "بگو").replace("بگیم", "بگو")
    d = re.sub(r"\s+", " ", d).strip(" .،,")
    if d and not d.endswith("."):
        d += "."
    return d


def _sanitize_title(title: str | None) -> str | None:
    if not title:
        return None
    title = clean_work_text(title)
    title = re.sub(r"^من\s+باید\s*", "", title)
    title = clean_work_text(title)
    if len(title) < 3:
        return None
    if "ذخیره کن" in title or title in {"ذخیره", "ذخیره کن"}:
        return None
    return title


def _heuristic_extract(
    text: str, *, speaker_key: str | None, today: date
) -> UnderstandResult:
    if _is_meta_followup(text) and not _looks_like_task_payload(text):
        return UnderstandResult(confidence="low")

    t = _strip_mentions(text)
    due = _parse_due_in_text(t, today)
    t = _strip_due_phrases(t)

    description: str | None = None
    dm = re.search(r"(?:و\s+)?(اگر\s+.+)$", t)
    if dm:
        description = _normalize_desc(dm.group(1))
        t = t[: dm.start()].strip()

    t = re.sub(r"\s+", " ", t).strip(" .،,")
    prefix = _TASK_PREFIX.match(t)
    first_person = _is_first_person(text)

    if prefix:
        main = prefix.group(1).strip(" .،,")
        title = _title_from_main(main)
    elif first_person:
        mm = re.search(r"(?:من\s+)?باید\s+(.+)", t)
        main = mm.group(1).strip(" .،,") if mm else t
        title = _title_from_main(main)
    elif _BAYAD_SHAVAD.match(clean_work_text(t)) or _BAYAD_SHAVAD.match(t):
        title = _title_from_main(t)
    else:
        title = _title_from_main(t)

    title = _sanitize_title(title)

    if first_person and speaker_key:
        assignee: str | None = speaker_key
    else:
        assignee = _explicit_assignee(text)
        if first_person and speaker_key:
            assignee = speaker_key

    work_intent = bool(title) and (
        first_person or "باید" in text or bool(prefix) or _looks_like_task_payload(text)
    )
    if not work_intent:
        title = None

    return UnderstandResult(
        title=title,
        description=description,
        assignee_key=assignee,
        due_date=due,
        confidence="low",
    )


def _assignee_from_text(text: str, speaker_key: str | None) -> str | None:
    if _is_first_person(text) and speaker_key:
        return speaker_key
    found = _explicit_assignee(text)
    if found and found in MEMBER_BY_KEY:
        return found
    return None


def _finalize(
    result: UnderstandResult, text: str, speaker_key: str | None
) -> UnderstandResult:
    title = _sanitize_title(result.title)
    assignee = result.assignee_key if result.assignee_key in MEMBER_BY_KEY else None
    # First-person obligation always belongs to the speaker, never the mentioned object-owner.
    assignee = _assignee_from_text(text, speaker_key) or (
        None if _is_first_person(text) else assignee
    )
    if _is_first_person(text) and speaker_key:
        assignee = speaker_key

    has_title = bool(title)
    has_owner = bool(assignee)
    has_due = result.due_date is not None
    work_intent = has_title or (
        _looks_like_task_payload(text) and not _is_meta_followup(text)
    )

    ask: str | None = None
    confidence = "low"
    if has_title and has_owner and has_due:
        confidence = "high"
    elif work_intent or has_title:
        if not has_owner and not has_due:
            ask = ASK_BOTH
        elif not has_owner:
            ask = ASK_OWNER
        elif not has_due:
            ask = ASK_DUE

    return UnderstandResult(
        title=title,
        description=result.description,
        assignee_key=assignee,
        due_date=result.due_date,
        confidence=confidence,
        ask=ask,
    )


def _llm_credentials() -> tuple[str, str] | None:
    base = (os.environ.get("CHARBOT_LLM_BASE_URL") or "").strip()
    key = (
        os.environ.get("CHARBOT_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    ).strip()
    if not base or not key:
        return None
    return base, key


def _try_llm_extract(
    text: str, *, speaker_key: str | None, today: date
) -> dict[str, Any] | None:
    creds = _llm_credentials()
    if not creds:
        return None
    base, key = creds
    model = (os.environ.get("CHARBOT_LLM_MODEL") or "gpt-4o-mini").strip()
    url = base.rstrip("/")
    if not url.endswith("/chat/completions"):
        url = url + "/chat/completions"
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Extract one followable work task from colloquial Persian. "
                    "Return JSON only with keys title, description, assignee_key, "
                    "due_date, confidence, ask. "
                    "assignee_key must be kawe|hamed|saman|mohammadreza|ghazal or null. "
                    "due_date YYYY-MM-DD or null. "
                    "title: short, no fillers (من باید / ذخیره کن). "
                    "A name after متعلق به or به X بگو is NOT the assignee. "
                    f"First-person من باید…کنم assigns speaker_key={speaker_key!s}. "
                    f"today={today.isoformat()}."
                ),
            },
            {"role": "user", "content": text},
        ],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        logger.info("LLM extract skipped; heuristics only")
        return None
    try:
        data = json.loads(raw)
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(content, str):
        return None
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_iso_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    s = str(value).strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _merge_llm(
    heuristic: UnderstandResult, llm: dict[str, Any] | None, today: date
) -> UnderstandResult:
    del today
    if not llm:
        return heuristic
    title = heuristic.title
    raw_title = llm.get("title")
    if isinstance(raw_title, str):
        cleaned = _sanitize_title(raw_title)
        if cleaned:
            title = cleaned
    desc = heuristic.description
    raw_desc = llm.get("description")
    if isinstance(raw_desc, str) and raw_desc.strip():
        desc = _normalize_desc(raw_desc)
    assignee = heuristic.assignee_key
    raw_assignee = llm.get("assignee_key")
    if isinstance(raw_assignee, str) and raw_assignee in MEMBER_BY_KEY:
        assignee = raw_assignee
    due = heuristic.due_date
    llm_due = _parse_iso_date(llm.get("due_date"))
    if llm_due:
        due = llm_due
    return UnderstandResult(
        title=title,
        description=desc,
        assignee_key=assignee,
        due_date=due,
        confidence="low",
    )
