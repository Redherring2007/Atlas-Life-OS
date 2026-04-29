from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from telegram.ext import Application

from config import config
from db import fetch_due_reminder_tasks, mark_reminder_sent


def _format_due(value: str | None) -> str:
    if not value:
        return "No due date"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return value


async def reminder_worker(application: Application, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            tasks = await asyncio.to_thread(fetch_due_reminder_tasks)
            for task in tasks:
                message = (
                    "Reminder due\n\n"
                    f"{task['title']}\n"
                    f"Due: {_format_due(task.get('due_at'))}\n"
                    f"Category: {task.get('category', 'task')}\n"
                    f"Priority: {task.get('priority', 'medium')}"
                )
                try:
                    await application.bot.send_message(chat_id=task["telegram_chat_id"], text=message)
                    await asyncio.to_thread(mark_reminder_sent, task["id"])
                except Exception:
                    continue
        except Exception:
            pass

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=config.reminder_check_seconds)
        except asyncio.TimeoutError:
            continue
