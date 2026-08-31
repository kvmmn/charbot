"""Org glossary: names we were taught. Applied to ASR, replies, and stored titles."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

GLOSSARY_KV = "org.glossary"

_DEFAULT = (
    {
        "kind": "employer",
        "en": "JTI",
        "fa": "جی‌تی‌آی",
        "aliases": ["GTI", "G.T.I", "J.T.I", "جی تی آی", "جیتیآی"],
    },
    {
        "kind": "company",
        "en": "Chaharsotoon",
        "fa": "چهارستون",
        "aliases": ["چارسوتون", "4S"],
    },
    {
        "kind": "brand",
        "en": "SHEY",
        "fa": "شی",
        "aliases": ["Shey", "شیء"],
    },
)

_EN_FA = re.compile(
    r"به\s+انگلیسی\s+([A-Za-z][A-Za-z0-9.&/-]{0,40})\s+(?:و\s+)?به\s+فارسی\s+([^\s.،,]+)",
    re.IGNORECASE,
)
_FA_EN = re.compile(
    r"به\s+فارسی\s+([^\s.،,]+)\s+(?:و\s+)?به\s+انگلیسی\s+([A-Za-z][A-Za-z0-9.&/-]{0,40})",
    re.IGNORECASE,
)
_LEARN_HINTS = (
    "درست استفاده",
    "درست استفاده‌ش",
    "به انگلیسی",
    "به فارسی",
    "اسم کارفرما",
    "اسم شرکت",
    "اسم برند",
    "اسم پروژه",
    "از این به بعد",
    "غلط ننویس",
    "اشتباه ننویس",
    "یاد بگیر",
    "فراموش نکن",
)


@dataclass
class GlossaryEntry:
    kind: str
    en: str
    fa: str
    aliases: list[str] = field(default_factory=list)

    def all_forms(self) -> list[str]:
        forms = [self.en, self.fa, *self.aliases]
        out: list[str] = []
        seen: set[str] = set()
        for f in forms:
            t = (f or "").strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        return out


def is_learn_utterance(text: str) -> bool:
    t = text or ""
    if any(h in t for h in _LEARN_HINTS):
        return True
    if _EN_FA.search(t) or _FA_EN.search(t):
        return True
    return False


def extract_glossary_entries(text: str) -> list[GlossaryEntry]:
    t = text or ""
    found: list[GlossaryEntry] = []
    kind = "name"
    if "کارفرما" in t:
        kind = "employer"
    elif "شرکت" in t:
        kind = "company"
    elif "برند" in t:
        kind = "brand"
    elif "پروژه" in t:
        kind = "project"
    for m in _EN_FA.finditer(t):
        en, fa = m.group(1).strip(), m.group(2).strip(" .،,")
        found.append(_with_typo_aliases(kind, en, fa))
    for m in _FA_EN.finditer(t):
        fa, en = m.group(1).strip(" .،,"), m.group(2).strip()
        found.append(_with_typo_aliases(kind, en, fa))
    return found


def _with_typo_aliases(kind: str, en: str, fa: str) -> GlossaryEntry:
    aliases: list[str] = []
    if en.upper() == "JTI":
        aliases.extend(["GTI", "G.T.I", "J.T.I"])
    if fa.replace("‌", "") in ("جیتیآی", "جیتیاِی"):
        aliases.append("جی تی آی")
    return GlossaryEntry(kind=kind, en=en, fa=fa, aliases=aliases)


def ack_learn(entries: list[GlossaryEntry]) -> str:
    if not entries:
        return "گرفتم. از این به بعد همان را رعایت می‌کنم."
    bits = []
    for e in entries:
        if e.kind == "employer":
            bits.append(f"کارفرما {e.en} / {e.fa}")
        else:
            bits.append(f"{e.en} / {e.fa}")
    return "اوکی. " + "، ".join(bits) + " می‌نویسم."


def apply_text(text: str, entries: list[GlossaryEntry]) -> str:
    out = text or ""
    for e in entries:
        canonical = e.en or e.fa
        for alias in e.aliases:
            if not alias or alias.lower() == canonical.lower():
                continue
            out = re.sub(re.escape(alias), canonical, out, flags=re.IGNORECASE)
    return out


def asr_prompt(entries: list[GlossaryEntry] | None = None) -> str:
    names = [
        "چهارستون",
        "شی SHEY",
        "مشهد",
        "فرجی",
        "فرهمند",
        "JTI جی‌تی‌آی",
        "امام خمینی",
        "مهرآباد",
        "غزل",
        "حامد",
        "سامان",
        "محمدرضا",
        "کاوه",
    ]
    extra = []
    for e in entries or []:
        extra.append(f"{e.en} {e.fa}".strip())
    blob = "، ".join(names + extra)
    return f"Persian speech. Proper names: {blob}."


def default_entries() -> list[GlossaryEntry]:
    return [GlossaryEntry(**d) for d in _DEFAULT]


def load_entries(store) -> list[GlossaryEntry]:
    raw = store.get_kv(GLOSSARY_KV) if store is not None else None
    items: list[dict] = []
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                items = [x for x in parsed if isinstance(x, dict)]
        except json.JSONDecodeError:
            items = []
    by_en = {d["en"].upper(): d for d in _DEFAULT if d.get("en")}
    for it in items:
        en = (it.get("en") or "").upper()
        if en:
            by_en[en] = {**by_en.get(en, {}), **it}
    return [GlossaryEntry(
        kind=str(d.get("kind") or "name"),
        en=str(d.get("en") or ""),
        fa=str(d.get("fa") or ""),
        aliases=list(d.get("aliases") or []),
    ) for d in by_en.values()]


def save_entries(store, entries: list[GlossaryEntry]) -> None:
    payload = [
        {"kind": e.kind, "en": e.en, "fa": e.fa, "aliases": e.aliases}
        for e in entries
    ]
    store.set_kv(GLOSSARY_KV, json.dumps(payload, ensure_ascii=False))


def upsert_entries(store, new: list[GlossaryEntry]) -> list[GlossaryEntry]:
    current = load_entries(store)
    by = {(e.en or e.fa).upper(): e for e in current}
    for e in new:
        key = (e.en or e.fa).upper()
        old = by.get(key)
        if old:
            aliases = list({*old.aliases, *e.aliases})
            by[key] = GlossaryEntry(
                kind=e.kind or old.kind,
                en=e.en or old.en,
                fa=e.fa or old.fa,
                aliases=aliases,
            )
        else:
            by[key] = e
    merged = list(by.values())
    save_entries(store, merged)
    return merged


def apply_to_open_tasks(
    store, group_id: int, entries: list[GlossaryEntry]
) -> list[tuple[int, str, str]]:
    """Rewrite alias spellings in open titles/descriptions."""
    changed: list[tuple[int, str, str]] = []
    if not entries:
        return changed
    for task in store.list_open_tasks(group_id):
        new_title = apply_text(task.title, entries)
        old_desc = task.description or ""
        new_desc = apply_text(old_desc, entries)
        if new_title == task.title and new_desc == old_desc:
            continue
        store.update_task_fields(
            task.id,
            group_id,
            title=new_title,
            description=new_desc if new_desc else None,
        )
        changed.append((task.id, task.title, new_title))
    return changed
