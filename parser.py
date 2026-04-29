from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import dateparser
from dateparser.search import search_dates

from config import config


CATEGORIES = {
    "task",
    "reminder",
    "payment_follow_up",
    "lead_follow_up",
    "contract_action",
    "personal_reminder",
}
PRIORITIES = {"low", "medium", "high"}


@dataclass(frozen=True)
class ParsedTask:
    title: str
    due_at: str | None
    category: str
    priority: str


def _utc_iso(dt: datetime | None) -> str | None:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _clean_title(text: str) -> str:
    title = re.sub(r"\s+", " ", text).strip(" .")
    return title[:240] or "Untitled task"


def _fallback_due_at(text: str) -> str | None:
    settings = {
        "PREFER_DATES_FROM": "future",
        "RETURN_AS_TIMEZONE_AWARE": True,
        "TIMEZONE": "UTC",
        "TO_TIMEZONE": "UTC",
    }
    matches = search_dates(text, settings=settings)
    if matches:
        return _utc_iso(matches[-1][1])
    parsed = dateparser.parse(text, settings=settings)
    return _utc_iso(parsed)


def _fallback_category(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in ("invoice", "payment", "paid", "overdue", "chase payment", "follow up payment")):
        return "payment_follow_up"
    if any(word in lowered for word in ("lead", "prospect", "sales call", "demo", "crm")):
        return "lead_follow_up"
    if any(word in lowered for word in ("contract", "agreement", "nda", "signature", "sign off", "terms")):
        return "contract_action"
    if any(word in lowered for word in ("remind me", "personal", "birthday", "doctor", "dentist", "family")):
        return "personal_reminder"
    if any(word in lowered for word in ("remind", "remember", "alarm")):
        return "reminder"
    return "task"


def _fallback_priority(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in ("urgent", "asap", "immediately", "critical", "high priority", "important")):
        return "high"
    if any(word in lowered for word in ("low priority", "whenever", "no rush", "sometime")):
        return "low"
    return "medium"


def fallback_parse_task(text: str) -> ParsedTask:
    return ParsedTask(
        title=_clean_title(text),
        due_at=_fallback_due_at(text),
        category=_fallback_category(text),
        priority=_fallback_priority(text),
    )


def _validate_openai_payload(payload: dict[str, Any], original: str) -> ParsedTask:
    title = _clean_title(str(payload.get("title") or original))
    due_at_value = payload.get("due_at")
    due_at = None
    if due_at_value:
        parsed = dateparser.parse(str(due_at_value), settings={"RETURN_AS_TIMEZONE_AWARE": True, "TIMEZONE": "UTC", "TO_TIMEZONE": "UTC"})
        due_at = _utc_iso(parsed)
    category = str(payload.get("category") or "task")
    priority = str(payload.get("priority") or "medium")
    if category not in CATEGORIES:
        category = "task"
    if priority not in PRIORITIES:
        priority = "medium"
    return ParsedTask(title=title, due_at=due_at, category=category, priority=priority)


async def _openai_parse_task(text: str) -> ParsedTask:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=config.openai_api_key)
    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "You convert short user messages into strict JSON tasks. "
                    "Return only keys: title, due_at, category, priority. "
                    "due_at must be ISO 8601 with timezone or null. "
                    "category must be one of task, reminder, payment_follow_up, "
                    "lead_follow_up, contract_action, personal_reminder. "
                    "priority must be low, medium, or high."
                ),
            },
            {"role": "user", "content": text},
        ],
        temperature=0,
    )
    content = response.choices[0].message.content or "{}"
    return _validate_openai_payload(json.loads(content), text)


async def parse_task(text: str) -> ParsedTask:
    if config.openai_api_key:
        try:
            return await _openai_parse_task(text)
        except Exception:
            pass
    return fallback_parse_task(text)
