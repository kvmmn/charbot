"""Colleague loop for directed speech.

Inspired by (not copied from) cascade voice agents: Listen → Understand →
Reason → Respond. Policy sits in front of tools, like a gateway: questions
never create tasks, writes are allowlisted, LLM output is data not code.

This is not telephony and not MCP-in-Telegram. Tools are in-process Python
with least privilege. If the LLM is down, heuristics still reply.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from charbot.glossary import (
    ack_learn,
    apply_to_open_tasks,
    extract_glossary_entries,
    upsert_entries,
)
from charbot.intent import SpeechAct, SpeechActKind, may_create_task, must_reply
from charbot.store import TaskStore

logger = logging.getLogger(__name__)

ALLOWED_TOOLS = frozenset({"reply", "learn_glossary", "ask"})
OPENROUTER_CHAT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-4o-mini"


@dataclass
class ColleagueResult:
    reply: str
    tools: list[str] = field(default_factory=list)
    source: str = "heuristic"


def run_colleague(
    store: TaskStore,
    *,
    group_id: int,
    text: str,
    act: SpeechAct,
    speaker_key: str | None = None,
) -> ColleagueResult:
    """Always return a user-visible reply for directed speech."""
    result = _heuristic(store, group_id, text, act)
    if act.kind in (SpeechActKind.LEARN, SpeechActKind.CHECKIN):
        _audit(store, speaker_key, text, result)
        return result
    llm = _try_llm(text, act)
    if llm is not None:
        result = _apply_llm_plan(store, group_id, text, llm)
    _audit(store, speaker_key, text, result)
    return result


def _heuristic(
    store: TaskStore, group_id: int, text: str, act: SpeechAct
) -> ColleagueResult:
    if act.kind == SpeechActKind.LEARN:
        entries = extract_glossary_entries(text)
        if entries:
            upsert_entries(store, entries)
            apply_to_open_tasks(store, group_id, entries)
        return ColleagueResult(ack_learn(entries), ["learn_glossary"], "heuristic")
    if act.kind == SpeechActKind.CHECKIN:
        return ColleagueResult("آره.", ["reply"], "heuristic")
    if must_reply(act, text):
        return ColleagueResult("آره، گوش می‌دهم.", ["reply"], "heuristic")
    return ColleagueResult("آره، گوش می‌دهم.", ["reply"], "heuristic")


def _try_llm(text: str, act: SpeechAct) -> dict | None:
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        return None
    model = (os.environ.get("CHARBOT_LLM_MODEL") or DEFAULT_MODEL).strip()
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are charbot, a colleague in the company Telegram group. "
                    "Reply in short natural Persian. JSON only: "
                    '{"tool":"reply|learn_glossary|ask","reply":"...","en":null,'
                    '"fa":null,"kind":null}. '
                    "learn_glossary when they teach a name (English/Persian). "
                    "ask when a field is missing. reply for check-ins like اوکی؟. "
                    "Never create a task. Never execute code. Never invent tools."
                ),
            },
            {
                "role": "user",
                "content": f"speech_act={act.kind.value}\ntext={text}",
            },
        ],
    }
    req = urllib.request.Request(
        OPENROUTER_CHAT,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
            "HTTP-Referer": "https://github.com/kvmmn/charbot",
            "X-Title": "charbot",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        logger.info("colleague LLM skipped; heuristic reply")
        return None
    try:
        content = raw["choices"][0]["message"]["content"]
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end <= start:
            return None
        plan = json.loads(content[start : end + 1])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(plan, dict):
        return None
    tool = str(plan.get("tool") or "reply")
    if tool not in ALLOWED_TOOLS:
        plan["tool"] = "reply"
    if may_create_task(text):
        # Fast-path create already handled; LLM must not insert here.
        return None
    return plan


def _apply_llm_plan(
    store: TaskStore, group_id: int, text: str, plan: dict
) -> ColleagueResult:
    tool = str(plan.get("tool") or "reply")
    reply = str(plan.get("reply") or "").strip()
    if tool == "learn_glossary":
        entries = extract_glossary_entries(text)
        if not entries and plan.get("en") and plan.get("fa"):
            from charbot.glossary import GlossaryEntry

            entries = [
                GlossaryEntry(
                    kind=str(plan.get("kind") or "name"),
                    en=str(plan["en"]),
                    fa=str(plan["fa"]),
                    aliases=[],
                )
            ]
        if entries:
            upsert_entries(store, entries)
            apply_to_open_tasks(store, group_id, entries)
            return ColleagueResult(
                reply or ack_learn(entries), ["learn_glossary"], "llm"
            )
    if not reply:
        reply = "آره، گوش می‌دهم."
    return ColleagueResult(reply, [tool if tool in ALLOWED_TOOLS else "reply"], "llm")


def _audit(
    store: TaskStore,
    speaker_key: str | None,
    text: str,
    result: ColleagueResult,
) -> None:
    payload = {
        "tools": result.tools,
        "source": result.source,
        "preview": (text or "")[:200],
    }
    try:
        if speaker_key:
            store.log_person_event(speaker_key, "colleague_act", payload=payload)
    except Exception:
        logger.debug("colleague audit skipped", exc_info=True)
