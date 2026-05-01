from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

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

TIME_WORDS = r"(?:today|tomorrow|tonight|morning|afternoon|evening|monday|tuesday|wednesday|thursday|friday|saturday|sunday|am|pm|a\.m\.|p\.m\.|minutes?|hours?|days?|weeks?)"
DATE_CONTEXT = r"\b(?:today|tomorrow|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday|next week|in \d+\s+(?:minutes?|hours?|days?|weeks?))\b"
TIME_CUE = r"(?:at|by|for|around|about|before|after)"
MERIDIEM = r"(?:[ap]\.?\s?m\.?)"
WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


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


def _dateparser_settings(local_timezone: str) -> dict[str, Any]:
    return {
        "PREFER_DATES_FROM": "future",
        "RETURN_AS_TIMEZONE_AWARE": True,
        "RELATIVE_BASE": datetime.now(ZoneInfo(local_timezone)),
        "TIMEZONE": local_timezone,
        "TO_TIMEZONE": "UTC",
    }


def _normalize_time_text(text: str) -> str:
    normalized = re.sub(r"\b(\d{1,2})(\d{2})\s*([ap])\.?m\.?\b", r"\1:\2 \3m", text, flags=re.IGNORECASE)
    normalized = re.sub(r"\b(\d{1,2})\s*([ap])\.?\s*m\.?\b", r"\1 \2m", normalized, flags=re.IGNORECASE)
    return normalized


def _has_date_context(text: str) -> bool:
    return bool(re.search(DATE_CONTEXT, text, flags=re.IGNORECASE))


def _dateparser_due_at(text: str, local_timezone: str) -> str | None:
    settings = _dateparser_settings(local_timezone)
    matches = search_dates(text, settings=settings)
    if matches:
        return _utc_iso(matches[-1][1])
    parsed = dateparser.parse(text, settings=settings)
    return _utc_iso(parsed)


def _normalized_meridiem(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"[^ap]", "", value.lower())
    return f"{cleaned}m" if cleaned in {"a", "p"} else None


def _coerce_clock(hour: int, minute: int, meridiem: str | None, local_timezone: str) -> str | None:
    if hour > 23 or minute > 59:
        return None
    meridiem = _normalized_meridiem(meridiem)
    if meridiem:
        if hour > 12:
            return None
        if meridiem == "pm" and hour != 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
    local_tz = ZoneInfo(local_timezone)
    now = datetime.now(local_tz)
    due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if due <= now:
        due += timedelta(days=1)
    return _utc_iso(due)


def _clock_components(text: str) -> tuple[int, int, str | None] | None:
    patterns = [
        rf"\b{TIME_CUE}\s+(?P<hour>\d{{1,2}}):(?P<minute>\d{{2}})\s*(?P<meridiem>{MERIDIEM})?\b",
        rf"\b{TIME_CUE}\s+(?P<hhmm>\d{{3,4}})\s*(?P<meridiem>{MERIDIEM})?\b",
        rf"\b{TIME_CUE}\s+(?P<hour>\d{{1,2}})\s*(?P<meridiem>{MERIDIEM})\b",
        rf"\b(?P<hour>\d{{1,2}}):(?P<minute>\d{{2}})\s*(?P<meridiem>{MERIDIEM})\b",
        rf"\b(?P<hhmm>\d{{3,4}})\s*(?P<meridiem>{MERIDIEM})\b",
        rf"\b(?P<hour>\d{{1,2}})\s*(?P<meridiem>{MERIDIEM})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        values = match.groupdict()
        if values.get("hhmm"):
            digits = values["hhmm"]
            hour = int(digits[:-2])
            minute = int(digits[-2:])
        else:
            hour = int(values["hour"])
            minute = int(values.get("minute") or 0)
        if hour <= 23 and minute <= 59:
            return hour, minute, values.get("meridiem")
    return None


def _coerced_clock_values(hour: int, minute: int, meridiem: str | None) -> tuple[int, int] | None:
    if hour > 23 or minute > 59:
        return None
    meridiem = _normalized_meridiem(meridiem)
    if meridiem:
        if hour > 12:
            return None
        if meridiem == "pm" and hour != 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
    return hour, minute


def _weekday_due_at(text: str, local_timezone: str) -> str | None:
    match = re.search(r"\b(" + "|".join(WEEKDAYS) + r")\b", text, flags=re.IGNORECASE)
    if not match:
        return None
    local_tz = ZoneInfo(local_timezone)
    now = datetime.now(local_tz)
    target_weekday = WEEKDAYS[match.group(1).lower()]
    days_ahead = (target_weekday - now.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    clock = _clock_components(text)
    if clock:
        values = _coerced_clock_values(*clock)
        if not values:
            return None
        hour, minute = values
    else:
        hour, minute = 9, 0
    due = (now + timedelta(days=days_ahead)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return _utc_iso(due)


def _clock_due_at(text: str, local_timezone: str) -> str | None:
    clock = _clock_components(text)
    if not clock:
        return None
    hour, minute, meridiem = clock
    return _coerce_clock(hour, minute, meridiem, local_timezone)


def _clean_title(text: str) -> str:
    title = _normalize_time_text(text)
    title = re.sub(r"\b(?:remind me to|remind me|remember to|please remind me to|please remind me|i need to|need to|can you remind me to)\b", "", title, flags=re.IGNORECASE)
    title = re.sub(rf"\b{TIME_CUE}\s+(?:\d{{1,2}}(?::\d{{2}})?|\d{{3,4}})\s*(?:{MERIDIEM})?\b", "", title, flags=re.IGNORECASE)
    title = re.sub(rf"\b(?:at|by|before|after|on|in)\s+[^,.!?]*(?:{TIME_WORDS}|\d{{1,2}}(?::\d{{2}})?\s*(?:{MERIDIEM}))\b", "", title, flags=re.IGNORECASE)
    title = re.sub(rf"\b\d{{1,2}}\s*(?:{MERIDIEM})\b", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+", " ", title).strip(" .,-")
    return title[:240] or "Untitled task"


def _fallback_due_at(text: str, local_timezone: str) -> str | None:
    normalized = _normalize_time_text(text)
    weekday_due = _weekday_due_at(normalized, local_timezone)
    if weekday_due:
        return weekday_due
    if _has_date_context(normalized):
        parsed_due = _dateparser_due_at(normalized, local_timezone)
        if parsed_due:
            return parsed_due
    clock_due = _clock_due_at(normalized, local_timezone)
    if clock_due:
        return clock_due
    return _dateparser_due_at(normalized, local_timezone)


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


def _friendly_day_label(due_at: str | None, local_timezone: str) -> str:
    if not due_at:
        return ""
    try:
        due = datetime.fromisoformat(due_at.replace("Z", "+00:00")).astimezone(ZoneInfo(local_timezone))
    except ValueError:
        return ""
    today = datetime.now(ZoneInfo(local_timezone)).date()
    if due.date() == today:
        return " today"
    if due.date() == today + timedelta(days=1):
        return " tomorrow"
    return ""


def _personal_title(title: str, due_at: str | None, local_timezone: str) -> str:
    cleaned = re.sub(r"\s+", " ", title).strip()
    lowered = cleaned.lower()
    for prefix in ("go to ", "get to "):
        if lowered.startswith(prefix):
            place = cleaned[len(prefix):].strip()
            if place:
                return f"You have {place}{_friendly_day_label(due_at, local_timezone)}"[:240]
    if lowered in {"school", "work", "gym", "class", "college", "university"}:
        return f"You have {cleaned}{_friendly_day_label(due_at, local_timezone)}"[:240]
    return cleaned[:240] or "Untitled task"


def fallback_parse_task(text: str, local_timezone: str) -> ParsedTask:
    normalized = _normalize_time_text(text)
    due_at = _fallback_due_at(normalized, local_timezone)
    title = _personal_title(_clean_title(normalized), due_at, local_timezone)
    return ParsedTask(
        title=title,
        due_at=due_at,
        category=_fallback_category(normalized),
        priority=_fallback_priority(normalized),
    )


def _validate_openai_payload(payload: dict[str, Any], original: str, local_timezone: str) -> ParsedTask:
    title = _clean_title(str(payload.get("title") or original))
    normalized_original = _normalize_time_text(original)
    explicit_due_at = _fallback_due_at(normalized_original, local_timezone)
    due_at_value = payload.get("due_at")
    due_at = explicit_due_at
    if due_at_value and not due_at:
        due_at_text = _normalize_time_text(str(due_at_value))
        due_at = _fallback_due_at(due_at_text, local_timezone)
    category = str(payload.get("category") or "task")
    priority = str(payload.get("priority") or "medium")
    if category not in CATEGORIES:
        category = "task"
    if priority not in PRIORITIES:
        priority = "medium"
    title = _personal_title(title, due_at, local_timezone)
    return ParsedTask(title=title, due_at=due_at, category=category, priority=priority)


async def _openai_parse_task(text: str, local_timezone: str) -> ParsedTask:
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
                    f"Interpret relative dates and times in the user's local timezone: {local_timezone}. "
                    "If the input is a noisy voice transcript, infer the most likely clean task title. "
                    "Compact times such as 1137 mean 11:37, not a year or date. "
                    "Return only keys: title, due_at, category, priority. "
                    "due_at must be ISO 8601 with timezone or null. "
                    "category must be one of task, reminder, payment_follow_up, "
                    "lead_follow_up, contract_action, personal_reminder. "
                    "priority must be low, medium, or high."
                ),
            },
            {"role": "user", "content": _normalize_time_text(text)},
        ],
        temperature=0,
    )
    content = response.choices[0].message.content or "{}"
    return _validate_openai_payload(json.loads(content), text, local_timezone)


async def parse_task(text: str, local_timezone: str | None = None) -> ParsedTask:
    timezone_name = local_timezone or config.local_timezone
    if config.openai_api_key:
        try:
            return await _openai_parse_task(text, timezone_name)
        except Exception:
            pass
    return fallback_parse_task(text, timezone_name)
