from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from config import config
from db import (
    complete_task_by_number,
    create_task,
    delete_task_by_number,
    list_overdue_tasks,
    list_pending_tasks,
    list_today_tasks,
)
from parser import ParsedTask, parse_task
from reminders import reminder_worker
from voice import VoiceTranscriptionError, transcribe_voice_note


logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s %(message)s", level=logging.INFO)
logger = logging.getLogger("forwardtask")

REMINDER_STOP_EVENT: asyncio.Event | None = None
REMINDER_TASK: asyncio.Task | None = None


def _format_due(value: str | None) -> str:
    if not value:
        return "No due date"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return value


def _format_task_line(index: int, task: dict[str, Any]) -> str:
    due = _format_due(task.get("due_at"))
    return f"{index}. {task['title']} | Due: {due} | {task['category']} | {task['priority']}"


def _format_task_list(tasks: list[dict[str, Any]], empty_message: str) -> str:
    if not tasks:
        return empty_message
    return "\n".join(_format_task_line(index, task) for index, task in enumerate(tasks, start=1))


def _task_payload(update: Update, source_type: str, raw_input: str, transcribed_text: str | None, parsed: ParsedTask) -> dict[str, Any]:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        raise RuntimeError("Missing Telegram user or chat.")
    return {
        "telegram_user_id": str(user.id),
        "telegram_chat_id": str(chat.id),
        "source_type": source_type,
        "raw_input": raw_input,
        "transcribed_text": transcribed_text,
        "title": parsed.title,
        "due_at": parsed.due_at,
        "category": parsed.category,
        "priority": parsed.priority,
    }


def _confirmation(source_label: str, source_text: str, task: dict[str, Any]) -> str:
    return (
        "Task saved\n\n"
        f"{source_label}: {source_text}\n"
        f"Title: {task['title']}\n"
        f"Due: {_format_due(task.get('due_at'))}\n"
        f"Category: {task['category']}\n"
        f"Priority: {task['priority']}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "ForwardTask is ready. Send me a task as text or a voice note, and I will save it with a category, priority, and due date when I can find one."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Commands:\n"
        "/tasks - list pending tasks\n"
        "/today - list tasks due today\n"
        "/overdue - list overdue tasks\n"
        "/done <task number> - mark a task done\n"
        "/delete <task number> - delete a task"
    )


async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tasks = await asyncio.to_thread(list_pending_tasks, str(update.effective_user.id))
    await update.message.reply_text(_format_task_list(tasks, "No pending tasks."))


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tasks = await asyncio.to_thread(list_today_tasks, str(update.effective_user.id))
    await update.message.reply_text(_format_task_list(tasks, "No tasks due today."))


async def overdue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tasks = await asyncio.to_thread(list_overdue_tasks, str(update.effective_user.id))
    await update.message.reply_text(_format_task_list(tasks, "No overdue tasks."))


def _task_number(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if not context.args:
        return None
    try:
        return int(context.args[0])
    except ValueError:
        return None


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    number = _task_number(context)
    if number is None:
        await update.message.reply_text("Use /done <task number>.")
        return
    task = await asyncio.to_thread(complete_task_by_number, str(update.effective_user.id), number)
    if not task:
        await update.message.reply_text("I could not find that pending task number.")
        return
    await update.message.reply_text(f"Done: {task['title']}")


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    number = _task_number(context)
    if number is None:
        await update.message.reply_text("Use /delete <task number>.")
        return
    task = await asyncio.to_thread(delete_task_by_number, str(update.effective_user.id), number)
    if not task:
        await update.message.reply_text("I could not find that pending task number.")
        return
    await update.message.reply_text(f"Deleted: {task['title']}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text:
        return
    try:
        parsed = await parse_task(text)
        task = await asyncio.to_thread(create_task, _task_payload(update, "text", text, None, parsed))
        await update.message.reply_text(_confirmation("Input", text, task))
    except Exception:
        logger.exception("Failed to handle text message")
        await update.message.reply_text("I could not save that task. Please try again.")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        transcription = await transcribe_voice_note(update.message.voice)
    except VoiceTranscriptionError as exc:
        await update.message.reply_text(str(exc))
        return

    try:
        parsed = await parse_task(transcription)
        task = await asyncio.to_thread(create_task, _task_payload(update, "voice", "", transcription, parsed))
        await update.message.reply_text(_confirmation("Transcription", transcription, task))
    except Exception:
        logger.exception("Failed to handle voice task")
        await update.message.reply_text("I transcribed the voice note but could not save it as a task. Please try again.")


async def on_startup(application: Application) -> None:
    global REMINDER_STOP_EVENT, REMINDER_TASK
    REMINDER_STOP_EVENT = asyncio.Event()
    REMINDER_TASK = asyncio.create_task(reminder_worker(application, REMINDER_STOP_EVENT))


async def on_shutdown(application: Application) -> None:
    if REMINDER_STOP_EVENT:
        REMINDER_STOP_EVENT.set()
    if REMINDER_TASK:
        await REMINDER_TASK


def build_application() -> Application:
    application = (
        Application.builder()
        .token(config.telegram_bot_token)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("tasks", tasks_command))
    application.add_handler(CommandHandler("today", today_command))
    application.add_handler(CommandHandler("overdue", overdue_command))
    application.add_handler(CommandHandler("done", done_command))
    application.add_handler(CommandHandler("delete", delete_command))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return application


def main() -> None:
    application = build_application()
    logger.info("Starting ForwardTask with Telegram long polling")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
