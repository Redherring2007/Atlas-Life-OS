from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from timezonefinder import TimezoneFinder

from config import config
from db import (
    complete_task_by_id,
    complete_task_by_number,
    create_task,
    delete_task_by_number,
    ensure_schema,
    get_task_by_id,
    get_user_timezone,
    list_overdue_tasks,
    list_pending_tasks,
    list_today_tasks,
    set_user_timezone,
    snooze_task_by_id,
    update_task_by_id,
    update_task_fields_by_id,
)
from parser import ParsedTask, parse_task
from reminders import reminder_worker
from voice import VoiceTranscriptionError, transcribe_voice_note


APP_NAME = "Atlas Life OS"
TIMEZONE_FINDER = TimezoneFinder()
EDITING_TASK_KEY = "editing_task_id"
EDITING_MODE_KEY = "editing_mode"
MENU_CURRENT_TASKS = "📋 Current Tasks"
MENU_DUE_TODAY = "📅 Due Today"
MENU_UPDATE_TIME = "📍 Update Local Time"
MENU_BACK = "↩️ Back"

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s %(message)s", level=logging.INFO)
logger = logging.getLogger("atlas_life_os")

REMINDER_STOP_EVENT: asyncio.Event | None = None
REMINDER_TASK: asyncio.Task | None = None


def _format_due(value: str | None, local_timezone: str) -> str:
    if not value:
        return "No due time set"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ZoneInfo(local_timezone))
        return dt.strftime("%a %d %b, %I:%M %p").lstrip("0")
    except ValueError:
        return value


def _location_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Share location", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Share location to update local time...",
    )


def _edit_choice_buttons(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✏️ Task", callback_data=f"editmode:task:{task_id}")],
            [InlineKeyboardButton("⏰ Time", callback_data=f"editmode:time:{task_id}")],
            [InlineKeyboardButton("📝 Both", callback_data=f"editmode:both:{task_id}")],
            [InlineKeyboardButton("↩️ Back", callback_data="cancel_edit")],
        ]
    )


def _edit_cancel_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="cancel_edit")]])


def _task_card(task: dict[str, Any], local_timezone: str, heading: str = "Task", icon: str = "📝") -> str:
    return (
        f"{icon} {heading}\n\n"
        f"{task['title']}\n\n"
        f"⏰ Due\n"
        f"{_format_due(task.get('due_at'), local_timezone)}"
    )


def _task_buttons(task: dict[str, Any], include_snooze: bool = False) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton("✏️ Edit", callback_data=f"edit:{task['id']}"),
        InlineKeyboardButton("✅ Done", callback_data=f"done:{task['id']}"),
    ]]
    if include_snooze:
        rows.append([InlineKeyboardButton("⏰ Remind in 20 min", callback_data=f"snooze20:{task['id']}")])
    return InlineKeyboardMarkup(rows)


def _home_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📋 Current tasks", callback_data="tasks:pending")],
            [InlineKeyboardButton("📅 Due today", callback_data="tasks:today")],
            [InlineKeyboardButton("📍 Update local time", callback_data="time:update")],
        ]
    )


def _home_only_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Home", callback_data="home")]])


def _task_list_message(tasks: list[dict[str, Any]], title: str, empty_message: str, local_timezone: str) -> str:
    if not tasks:
        return empty_message
    lines = [f"📋 {title}", ""]
    for index, task in enumerate(tasks, start=1):
        lines.append(f"{index}. {task['title']}")
        lines.append(f"   ⏰ {_format_due(task.get('due_at'), local_timezone)}")
        lines.append("")
    return "\n".join(lines).strip()


def _task_list_buttons(tasks: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows = []
    for index, task in enumerate(tasks[:10], start=1):
        rows.append([
            InlineKeyboardButton(f"✏️ Edit {index}", callback_data=f"edit:{task['id']}"),
            InlineKeyboardButton(f"✅ Done {index}", callback_data=f"done:{task['id']}"),
        ])
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="tasks:pending")])
    rows.append([InlineKeyboardButton("↩️ Home", callback_data="home")])
    return InlineKeyboardMarkup(rows)


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


def _task_updates(text: str, parsed: ParsedTask) -> dict[str, Any]:
    return {
        "raw_input": text,
        "transcribed_text": None,
        "title": parsed.title,
        "due_at": parsed.due_at,
        "category": parsed.category,
        "priority": parsed.priority,
    }


async def _home_message(user_id: str) -> str:
    local_timezone = await asyncio.to_thread(get_user_timezone, user_id)
    pending, today = await asyncio.gather(
        asyncio.to_thread(list_pending_tasks, user_id),
        asyncio.to_thread(list_today_tasks, user_id),
    )
    return (
        f"🧭 {APP_NAME}\n\n"
        f"📋 Current tasks: {len(pending)}\n"
        f"📅 Due today: {len(today)}\n"
        f"📍 Local time: {local_timezone}\n\n"
        "Speak or type a task whenever you want to capture something."
    )


async def _send_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        await _home_message(str(update.effective_user.id)),
        reply_markup=_home_buttons(),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(EDITING_TASK_KEY, None)
    context.user_data.pop(EDITING_MODE_KEY, None)
    await _send_home(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🧭 Atlas Life OS commands\n\n"
        "/start - open Atlas Life OS\n"
        "/tasks - view current tasks\n"
        "/today - view tasks due today\n"
        "/overdue - view overdue tasks\n"
        "/done <task number> - mark a task done\n"
        "/delete <task number> - delete a task",
        reply_markup=_home_only_buttons(),
    )


async def _send_task_list(update: Update, tasks: list[dict[str, Any]], title: str, empty_message: str, local_timezone: str) -> None:
    await update.message.reply_text(
        _task_list_message(tasks, title, empty_message, local_timezone),
        reply_markup=_task_list_buttons(tasks) if tasks else _home_only_buttons(),
    )


async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    local_timezone, tasks = await asyncio.gather(
        asyncio.to_thread(get_user_timezone, user_id),
        asyncio.to_thread(list_pending_tasks, user_id),
    )
    await _send_task_list(update, tasks, "Current Tasks", "📋 No current tasks.", local_timezone)


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    local_timezone, tasks = await asyncio.gather(
        asyncio.to_thread(get_user_timezone, user_id),
        asyncio.to_thread(list_today_tasks, user_id),
    )
    await _send_task_list(update, tasks, "Due Today", "📅 Nothing due today.", local_timezone)


async def overdue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    local_timezone, tasks = await asyncio.gather(
        asyncio.to_thread(get_user_timezone, user_id),
        asyncio.to_thread(list_overdue_tasks, user_id),
    )
    await _send_task_list(update, tasks, "Overdue", "✅ No overdue tasks.", local_timezone)


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
        await update.message.reply_text("Use /done <task number>.", reply_markup=_home_only_buttons())
        return
    task = await asyncio.to_thread(complete_task_by_number, str(update.effective_user.id), number)
    if not task:
        await update.message.reply_text("That task is already done or no longer available.", reply_markup=_home_only_buttons())
        return
    await update.message.reply_text(f"✅ Done\n\n{task['title']}", reply_markup=_home_only_buttons())


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    number = _task_number(context)
    if number is None:
        await update.message.reply_text("Use /delete <task number>.", reply_markup=_home_only_buttons())
        return
    task = await asyncio.to_thread(delete_task_by_number, str(update.effective_user.id), number)
    if not task:
        await update.message.reply_text("That task is already done or no longer available.", reply_markup=_home_only_buttons())
        return
    await update.message.reply_text(f"🗑 Deleted\n\n{task['title']}", reply_markup=_home_only_buttons())


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    location = update.message.location
    timezone_name = TIMEZONE_FINDER.timezone_at(lat=location.latitude, lng=location.longitude)
    if not timezone_name:
        await update.message.reply_text("I could not detect a timezone from that location.", reply_markup=_home_only_buttons())
        return
    await asyncio.to_thread(set_user_timezone, str(update.effective_user.id), timezone_name)
    await update.message.reply_text(f"📍 Local time updated\n\n{timezone_name}", reply_markup=_home_only_buttons())


async def _handle_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, task_id: str, local_timezone: str) -> bool:
    mode = context.user_data.get(EDITING_MODE_KEY) or "both"
    parsed = await parse_task(text, local_timezone)
    if mode == "both":
        task = await asyncio.to_thread(update_task_by_id, str(update.effective_user.id), task_id, _task_updates(text, parsed))
    elif mode == "task":
        current = await asyncio.to_thread(get_task_by_id, str(update.effective_user.id), task_id)
        fields = {
            "raw_input": text,
            "transcribed_text": None,
            "title": parsed.title,
            "category": parsed.category,
            "priority": parsed.priority,
        }
        if current and not current.get("due_at") and parsed.due_at:
            fields["due_at"] = parsed.due_at
        task = await asyncio.to_thread(update_task_fields_by_id, str(update.effective_user.id), task_id, fields)
    else:
        task = await asyncio.to_thread(update_task_fields_by_id, str(update.effective_user.id), task_id, {"due_at": parsed.due_at})
    context.user_data.pop(EDITING_TASK_KEY, None)
    context.user_data.pop(EDITING_MODE_KEY, None)
    if not task:
        await update.message.reply_text("That task is already done or no longer available.", reply_markup=_home_only_buttons())
        return True
    await update.message.reply_text(_task_card(task, local_timezone, "Updated", "✅"), reply_markup=_task_buttons(task))
    return True


async def _handle_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    if text == MENU_BACK:
        context.user_data.pop(EDITING_TASK_KEY, None)
        context.user_data.pop(EDITING_MODE_KEY, None)
        await update.message.reply_text("↩️ Back to Atlas Life OS", reply_markup=_home_only_buttons())
        return True
    if text == MENU_CURRENT_TASKS:
        context.user_data.pop(EDITING_TASK_KEY, None)
        context.user_data.pop(EDITING_MODE_KEY, None)
        await tasks_command(update, context)
        return True
    if text == MENU_DUE_TODAY:
        context.user_data.pop(EDITING_TASK_KEY, None)
        context.user_data.pop(EDITING_MODE_KEY, None)
        await today_command(update, context)
        return True
    if text == MENU_UPDATE_TIME:
        context.user_data.pop(EDITING_TASK_KEY, None)
        context.user_data.pop(EDITING_MODE_KEY, None)
        await update.message.reply_text("📍 Tap the location button below to update local time.", reply_markup=_location_keyboard())
        return True
    return False


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text:
        return
    try:
        if await _handle_menu_text(update, context, text):
            return
        local_timezone = await asyncio.to_thread(get_user_timezone, str(update.effective_user.id))
        editing_task_id = context.user_data.get(EDITING_TASK_KEY)
        if editing_task_id:
            await _handle_edit_text(update, context, text, editing_task_id, local_timezone)
            return
        parsed = await parse_task(text, local_timezone)
        task = await asyncio.to_thread(create_task, _task_payload(update, "text", text, None, parsed))
        await update.message.reply_text(_task_card(task, local_timezone, "Saved", "✅"), reply_markup=_task_buttons(task))
    except Exception:
        logger.exception("Failed to handle text message")
        await update.message.reply_text("I could not save that task. Please try again.", reply_markup=_home_only_buttons())


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        transcription = await transcribe_voice_note(update.message.voice)
    except VoiceTranscriptionError as exc:
        await update.message.reply_text(str(exc), reply_markup=_home_only_buttons())
        return

    try:
        local_timezone = await asyncio.to_thread(get_user_timezone, str(update.effective_user.id))
        editing_task_id = context.user_data.get(EDITING_TASK_KEY)
        if editing_task_id:
            await _handle_edit_text(update, context, transcription, editing_task_id, local_timezone)
            return
        parsed = await parse_task(transcription, local_timezone)
        task = await asyncio.to_thread(create_task, _task_payload(update, "voice", "", transcription, parsed))
        await update.message.reply_text(_task_card(task, local_timezone, "Saved", "✅"), reply_markup=_task_buttons(task))
    except Exception:
        logger.exception("Failed to handle voice task")
        await update.message.reply_text("I transcribed the voice note but could not save it as a task. Please try again.", reply_markup=_home_only_buttons())


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    user_id = str(query.from_user.id)
    local_timezone = await asyncio.to_thread(get_user_timezone, user_id)

    if data == "home":
        context.user_data.pop(EDITING_TASK_KEY, None)
        context.user_data.pop(EDITING_MODE_KEY, None)
        await query.edit_message_text(await _home_message(user_id), reply_markup=_home_buttons())
        return

    if data == "cancel_edit":
        context.user_data.pop(EDITING_TASK_KEY, None)
        context.user_data.pop(EDITING_MODE_KEY, None)
        await query.edit_message_text(await _home_message(user_id), reply_markup=_home_buttons())
        return

    if data == "time:update":
        context.user_data.pop(EDITING_TASK_KEY, None)
        context.user_data.pop(EDITING_MODE_KEY, None)
        await query.edit_message_text("📍 Share your location to update local time.", reply_markup=_home_only_buttons())
        await query.message.reply_text("Location update", reply_markup=_location_keyboard())
        return

    if data == "tasks:pending":
        context.user_data.pop(EDITING_TASK_KEY, None)
        context.user_data.pop(EDITING_MODE_KEY, None)
        tasks = await asyncio.to_thread(list_pending_tasks, user_id)
        await query.edit_message_text(
            _task_list_message(tasks, "Current Tasks", "📋 No current tasks.", local_timezone),
            reply_markup=_task_list_buttons(tasks) if tasks else _home_only_buttons(),
        )
        return

    if data == "tasks:today":
        context.user_data.pop(EDITING_TASK_KEY, None)
        context.user_data.pop(EDITING_MODE_KEY, None)
        tasks = await asyncio.to_thread(list_today_tasks, user_id)
        await query.edit_message_text(
            _task_list_message(tasks, "Due Today", "📅 Nothing due today.", local_timezone),
            reply_markup=_task_list_buttons(tasks) if tasks else _home_only_buttons(),
        )
        return

    if data.startswith("editmode:"):
        _, mode, task_id = data.split(":", 2)
        context.user_data[EDITING_TASK_KEY] = task_id
        context.user_data[EDITING_MODE_KEY] = mode
        prompts = {
            "task": "✏️ Edit task\n\nSend the corrected task name. If this task has no due time yet, you can include one too.",
            "time": "⏰ Edit time\n\nSend the corrected due time only, for example: tomorrow at 4pm or 1137.",
            "both": "📝 Edit task and time\n\nSend the corrected task with its due time.",
        }
        await query.edit_message_text(prompts.get(mode, prompts["both"]), reply_markup=_edit_cancel_buttons())
        return

    action, _, task_id = data.partition(":")
    if action == "edit" and task_id:
        context.user_data[EDITING_TASK_KEY] = task_id
        context.user_data.pop(EDITING_MODE_KEY, None)
        await query.edit_message_text(
            "✏️ What do you want to edit?",
            reply_markup=_edit_choice_buttons(task_id),
        )
        return

    if action == "done" and task_id:
        task = await asyncio.to_thread(complete_task_by_id, user_id, task_id)
        context.user_data.pop(EDITING_TASK_KEY, None)
        context.user_data.pop(EDITING_MODE_KEY, None)
        if task:
            await query.edit_message_text(f"✅ Done\n\n{task['title']}", reply_markup=_home_only_buttons())
        else:
            await query.edit_message_text("That task is already done or no longer available.", reply_markup=_home_only_buttons())
        return

    if action == "snooze20" and task_id:
        task = await asyncio.to_thread(snooze_task_by_id, user_id, task_id, 20)
        if task:
            await query.edit_message_text(_task_card(task, local_timezone, "Remind again", "⏰"), reply_markup=_task_buttons(task))
        else:
            await query.edit_message_text("That task is already done or no longer available.", reply_markup=_home_only_buttons())


async def on_startup(application: Application) -> None:
    global REMINDER_STOP_EVENT, REMINDER_TASK
    await asyncio.to_thread(ensure_schema)
    logger.info("Database schema is ready")
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
    application.add_handler(CallbackQueryHandler(handle_button))
    application.add_handler(MessageHandler(filters.LOCATION, handle_location))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return application


def main() -> None:
    application = build_application()
    logger.info("Starting %s with Telegram long polling", APP_NAME)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
