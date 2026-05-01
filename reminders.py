from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application

from config import config
from db import fetch_due_reminder_tasks, mark_reminder_sent


def _format_due(value: str | None, local_timezone: str) -> str:
    if not value:
        return "No due time set"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ZoneInfo(local_timezone))
        return dt.strftime("%a %d %b, %I:%M %p").lstrip("0")
    except ValueError:
        return value


def _format_time(value: str | None, local_timezone: str) -> str:
    if not value:
        return "soon"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ZoneInfo(local_timezone))
        return dt.strftime("%-I:%M %p") if hasattr(dt, "strftime") else dt.strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return value


def _greeting(local_timezone: str) -> str:
    hour = datetime.now(ZoneInfo(local_timezone)).hour
    if 5 <= hour < 12:
        return "Morning"
    if 12 <= hour < 18:
        return "Hey"
    if 18 <= hour < 22:
        return "Evening"
    return "Hey"


def _task_phrase(title: str) -> str:
    phrase = title.strip()
    lowered = phrase.lower()
    if lowered.startswith("you have " ):
        phrase = phrase[9:]
        lowered = phrase.lower()
    for suffix in (" tomorrow", " today"):
        if lowered.endswith(suffix):
            phrase = phrase[: -len(suffix)]
            break
    if phrase.lower().startswith(("call ", "buy ", "send ", "check ", "pay ", "book ", "email ", "message ", "pick up ", "go to ")):
        return phrase
    return f"you have {phrase}"


def _reminder_message(task: dict) -> str:
    local_timezone = task.get("user_timezone") or config.local_timezone
    phrase = _task_phrase(task["title"])
    due_time = _format_time(task.get("due_at"), local_timezone)
    return (
        f"{_greeting(local_timezone)}. Don't forget, {phrase} at {due_time}.\n\n"
        f"Due\n"
        f"{_format_due(task.get('due_at'), local_timezone)}"
    )


def _reminder_buttons(task: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Done", callback_data=f"done:{task['id']}"),
                InlineKeyboardButton("⏰ Remind me later", callback_data=f"latermenu:{task['id']}"),
            ],
            [
                InlineKeyboardButton("15 min", callback_data=f"before:15:{task['id']}"),
                InlineKeyboardButton("30 min", callback_data=f"before:30:{task['id']}"),
            ],
            [
                InlineKeyboardButton("1 hr", callback_data=f"before:60:{task['id']}"),
                InlineKeyboardButton("1 day", callback_data=f"before:1440:{task['id']}"),
            ],
            [InlineKeyboardButton("📋 View tasks", callback_data="tasks:pending")],
        ]
    )


async def reminder_worker(application: Application, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            tasks = await asyncio.to_thread(fetch_due_reminder_tasks)
            for task in tasks:
                try:
                    await application.bot.send_message(
                        chat_id=task["telegram_chat_id"],
                        text=_reminder_message(task),
                        reply_markup=_reminder_buttons(task),
                    )
                    await asyncio.to_thread(mark_reminder_sent, task["id"])
                except Exception:
                    continue
        except Exception:
            pass

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=config.reminder_check_seconds)
        except asyncio.TimeoutError:
            continue
