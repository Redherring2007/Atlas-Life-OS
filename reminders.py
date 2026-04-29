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


def _reminder_message(task: dict) -> str:
    local_timezone = task.get("user_timezone") or config.local_timezone
    return f"Reminder\n\n{task['title']}\n\nDue\n{_format_due(task.get('due_at'), local_timezone)}"


def _reminder_buttons(task: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Mark done", callback_data=f"done:{task['id']}"),
                InlineKeyboardButton("Remind in 20 min", callback_data=f"snooze20:{task['id']}"),
            ],
            [InlineKeyboardButton("View current tasks", callback_data="tasks:pending")],
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
