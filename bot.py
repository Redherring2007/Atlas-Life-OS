from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, MenuButtonWebApp, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update, User, WebAppInfo
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from timezonefinder import TimezoneFinder

from config import config
from db import (
    clear_parking_location,
    complete_task_by_id,
    complete_task_by_number,
    create_task,
    delete_task_by_number,
    ensure_schema,
    ensure_user_access,
    get_active_parking,
    get_task_by_id,
    get_user_timezone,
    list_overdue_tasks,
    list_pending_tasks,
    list_today_tasks,
    restore_task_by_id,
    save_parking_location,
    set_parking_bay,
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
LOCATION_MODE_KEY = "location_mode"
PARKING_BAY_MODE_KEY = "parking_bay_mode"
MENU_CURRENT_TASKS = "📋 Current Tasks"
MENU_DUE_TODAY = "📅 Due Today"
MENU_UPDATE_TIME = "📍 Update Local Time"
MENU_BACK = "↩️ Back"
REPLY_KEYBOARD_REMOVED_KEY = "reply_keyboard_removed"
ACCESS_PAUSED_MESSAGE = "Atlas Life OS access is currently paused."

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


def _format_local_time(value: str | None, local_timezone: str) -> str:
    if not value:
        return "Just now"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ZoneInfo(local_timezone))
        return dt.strftime("%a %d %b, %I:%M %p").lstrip("0")
    except ValueError:
        return value


def _maps_url(parking: dict[str, Any]) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={parking['latitude']},{parking['longitude']}"


def _mini_app_url() -> str | None:
    if config.mini_app_url:
        return config.mini_app_url.rstrip("/")
    return None


async def _access_allowed(user: User | None) -> bool:
    if not user:
        return False
    return await asyncio.to_thread(
        ensure_user_access,
        str(user.id),
        user.username,
        user.first_name,
        user.last_name,
    )


async def _guard_message(update: Update) -> bool:
    allowed = await _access_allowed(update.effective_user)
    if not allowed and update.message:
        await update.message.reply_text(ACCESS_PAUSED_MESSAGE, reply_markup=ReplyKeyboardRemove())
    return allowed


async def _guard_query(update: Update) -> bool:
    query = update.callback_query
    allowed = await _access_allowed(query.from_user if query else None)
    if not allowed and query:
        await query.edit_message_text(ACCESS_PAUSED_MESSAGE)
    return allowed


def _location_keyboard(label: str = "📍 Share location") -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(label, request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Share location...",
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


def _undo_buttons(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Undo", callback_data=f"undo:{task_id}")]])


def _task_card(task: dict[str, Any], local_timezone: str, heading: str = "Task", icon: str = "📝") -> str:
    return (
        f"{icon} {heading}\n\n"
        f"{task['title']}\n\n"
        f"⏰ {_format_due(task.get('due_at'), local_timezone)}"
    )


def _task_buttons(task: dict[str, Any], include_snooze: bool = False) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton("✏️ Edit", callback_data=f"edit:{task['id']}"),
        InlineKeyboardButton("✅ Done", callback_data=f"done:{task['id']}"),
    ]]
    if include_snooze:
        rows.append([InlineKeyboardButton("⏰ Remind in 20 min", callback_data=f"snooze20:{task['id']}")])
    return InlineKeyboardMarkup(rows)


def _parking_status(parking: dict[str, Any] | None, local_timezone: str) -> str:
    if not parking:
        return "Not logged"
    bay = parking.get("bay_number") or "No bay added"
    saved = _format_local_time(parking.get("updated_at") or parking.get("created_at"), local_timezone)
    return f"Saved {saved} · {bay}"


def _parking_message(parking: dict[str, Any], local_timezone: str) -> str:
    bay = parking.get("bay_number")
    lines = [
        "🚘 Parked car",
        "",
        f"📍 Location saved",
        f"🕒 {_format_local_time(parking.get('updated_at') or parking.get('created_at'), local_timezone)}",
    ]
    if bay:
        lines.append(f"🅿️ Bay {bay}")
    return "\n".join(lines)


def _parking_buttons(parking: dict[str, Any]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🧭 Directions", url=_maps_url(parking))],
            [InlineKeyboardButton("🅿️ Add bay number", callback_data="parking:bay")],
            [InlineKeyboardButton("✅ Picked up car", callback_data="parking:clear")],
            [InlineKeyboardButton("↩️ Home", callback_data="home")],
        ]
    )


def _home_buttons(parking: dict[str, Any] | None) -> InlineKeyboardMarkup:
    rows = []
    mini_url = _mini_app_url()
    if mini_url:
        rows.append([InlineKeyboardButton("✨ Open Atlas App", web_app=WebAppInfo(url=f"{mini_url}/app"))])
    parking_label = "🚘 Find parked car" if parking else "🚘 Log parking"
    rows.extend(
        [
            [InlineKeyboardButton(parking_label, callback_data="parking")],
            [InlineKeyboardButton("📋 Current tasks", callback_data="tasks:pending")],
            [InlineKeyboardButton("📅 Due today", callback_data="tasks:today")],
            [InlineKeyboardButton("📍 Update local time", callback_data="time:update")],
        ]
    )
    return InlineKeyboardMarkup(rows)


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


async def _home_payload(user_id: str) -> tuple[str, InlineKeyboardMarkup]:
    local_timezone = await asyncio.to_thread(get_user_timezone, user_id)
    pending, today, parking = await asyncio.gather(
        asyncio.to_thread(list_pending_tasks, user_id),
        asyncio.to_thread(list_today_tasks, user_id),
        asyncio.to_thread(get_active_parking, user_id),
    )
    message = (
        f"🧭 {APP_NAME}\n\n"
        f"📋 Tasks {len(pending)}\n"
        f"📅 Today {len(today)}\n"
        f"🚘 Parking {_parking_status(parking, local_timezone)}\n"
        f"📍 {local_timezone}\n\n"
        "Add a task by typing it here or sending a voice note."
    )
    return message, _home_buttons(parking)


async def _send_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, buttons = await _home_payload(str(update.effective_user.id))
    await update.message.reply_text(message, reply_markup=buttons)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(EDITING_TASK_KEY, None)
    context.user_data.pop(EDITING_MODE_KEY, None)
    context.user_data.pop(LOCATION_MODE_KEY, None)
    context.user_data.pop(PARKING_BAY_MODE_KEY, None)
    if not await _guard_message(update):
        return
    if update.message and not context.user_data.get(REPLY_KEYBOARD_REMOVED_KEY):
        await update.message.reply_text("Atlas controls refreshed.", reply_markup=ReplyKeyboardRemove())
        context.user_data[REPLY_KEYBOARD_REMOVED_KEY] = True
    await _send_home(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_message(update):
        return
    await update.message.reply_text(
        "🧭 Atlas Life OS commands\n\n"
        "/home - open Atlas Life OS\n"
        "/tasks - view current tasks\n"
        "/today - view tasks due today\n"
        "/overdue - view overdue tasks\n"
        "/done <task number> - mark a task done\n"
        "/delete <task number> - delete a task"
    )


async def _send_task_list(update: Update, tasks: list[dict[str, Any]], title: str, empty_message: str, local_timezone: str) -> None:
    await update.message.reply_text(
        _task_list_message(tasks, title, empty_message, local_timezone),
        reply_markup=_task_list_buttons(tasks) if tasks else None,
    )


async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_message(update):
        return
    user_id = str(update.effective_user.id)
    local_timezone, tasks = await asyncio.gather(
        asyncio.to_thread(get_user_timezone, user_id),
        asyncio.to_thread(list_pending_tasks, user_id),
    )
    await _send_task_list(update, tasks, "Current Tasks", "📋 No current tasks.", local_timezone)


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_message(update):
        return
    user_id = str(update.effective_user.id)
    local_timezone, tasks = await asyncio.gather(
        asyncio.to_thread(get_user_timezone, user_id),
        asyncio.to_thread(list_today_tasks, user_id),
    )
    await _send_task_list(update, tasks, "Due Today", "📅 Nothing due today.", local_timezone)


async def overdue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_message(update):
        return
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
    if not await _guard_message(update):
        return
    number = _task_number(context)
    if number is None:
        await update.message.reply_text("Use /done <task number>.")
        return
    task = await asyncio.to_thread(complete_task_by_number, str(update.effective_user.id), number)
    if not task:
        await update.message.reply_text("That task is already done or no longer available.")
        return
    await update.message.reply_text(f"✅ Done\n\n{task['title']}", reply_markup=_undo_buttons(task["id"]))


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_message(update):
        return
    number = _task_number(context)
    if number is None:
        await update.message.reply_text("Use /delete <task number>.")
        return
    task = await asyncio.to_thread(delete_task_by_number, str(update.effective_user.id), number)
    if not task:
        await update.message.reply_text("That task is already done or no longer available.")
        return
    await update.message.reply_text(f"🗑 Deleted\n\n{task['title']}")


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_message(update):
        return
    location = update.message.location
    user_id = str(update.effective_user.id)
    chat_id = str(update.effective_chat.id)
    mode = context.user_data.pop(LOCATION_MODE_KEY, None)
    if mode == "parking":
        parking = await asyncio.to_thread(save_parking_location, user_id, chat_id, location.latitude, location.longitude)
        context.user_data[PARKING_BAY_MODE_KEY] = True
        local_timezone = await asyncio.to_thread(get_user_timezone, user_id)
        await update.message.reply_text(
            _parking_message(parking, local_timezone) + "\n\nSend a bay number now, or tap Skip.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await update.message.reply_text(
            "Parking details",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🅿️ Skip bay", callback_data="parking:skip_bay")],
                    [InlineKeyboardButton("🧭 Directions", url=_maps_url(parking))],
                ]
            ),
        )
        return

    timezone_name = TIMEZONE_FINDER.timezone_at(lat=location.latitude, lng=location.longitude)
    if not timezone_name:
        await update.message.reply_text("I could not detect a timezone from that location.")
        return
    await asyncio.to_thread(set_user_timezone, user_id, timezone_name)
    await update.message.reply_text(f"📍 Local time updated\n\n{timezone_name}", reply_markup=ReplyKeyboardRemove())


async def _handle_parking_bay_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    if not context.user_data.get(PARKING_BAY_MODE_KEY):
        return False
    context.user_data.pop(PARKING_BAY_MODE_KEY, None)
    parking = await asyncio.to_thread(set_parking_bay, str(update.effective_user.id), text.strip())
    if not parking:
        await update.message.reply_text("No parked car is currently logged.")
        return True
    local_timezone = await asyncio.to_thread(get_user_timezone, str(update.effective_user.id))
    await update.message.reply_text(_parking_message(parking, local_timezone), reply_markup=_parking_buttons(parking))
    return True


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
        await update.message.reply_text("That task is already done or no longer available.")
        return True
    await update.message.reply_text(_task_card(task, local_timezone, "Updated", "✅"), reply_markup=_task_buttons(task))
    return True


async def _handle_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    if text == MENU_BACK:
        context.user_data.pop(EDITING_TASK_KEY, None)
        context.user_data.pop(EDITING_MODE_KEY, None)
        context.user_data.pop(LOCATION_MODE_KEY, None)
        context.user_data.pop(PARKING_BAY_MODE_KEY, None)
        await update.message.reply_text("↩️ Back to Atlas Life OS")
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
        context.user_data[LOCATION_MODE_KEY] = "timezone"
        await update.message.reply_text("📍 Share your location to update local time.", reply_markup=_location_keyboard())
        return True
    return False


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_message(update):
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    try:
        if await _handle_parking_bay_text(update, context, text):
            return
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
        await update.message.reply_text("I could not save that task. Please try again.")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_message(update):
        return
    try:
        transcription = await transcribe_voice_note(update.message.voice)
    except VoiceTranscriptionError as exc:
        await update.message.reply_text(str(exc))
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
        await update.message.reply_text("I transcribed the voice note but could not save it as a task. Please try again.")


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await _guard_query(update):
        return
    data = query.data or ""
    user_id = str(query.from_user.id)
    local_timezone = await asyncio.to_thread(get_user_timezone, user_id)

    if data == "home":
        context.user_data.pop(EDITING_TASK_KEY, None)
        context.user_data.pop(EDITING_MODE_KEY, None)
        context.user_data.pop(LOCATION_MODE_KEY, None)
        context.user_data.pop(PARKING_BAY_MODE_KEY, None)
        message, buttons = await _home_payload(user_id)
        await query.edit_message_text(message, reply_markup=buttons)
        return

    if data == "cancel_edit":
        context.user_data.pop(EDITING_TASK_KEY, None)
        context.user_data.pop(EDITING_MODE_KEY, None)
        message, buttons = await _home_payload(user_id)
        await query.edit_message_text(message, reply_markup=buttons)
        return

    if data == "time:update":
        context.user_data.pop(EDITING_TASK_KEY, None)
        context.user_data.pop(EDITING_MODE_KEY, None)
        context.user_data[LOCATION_MODE_KEY] = "timezone"
        await query.edit_message_text("📍 Share your location to update local time.")
        await query.message.reply_text("Location update", reply_markup=_location_keyboard())
        return

    if data == "parking":
        context.user_data.pop(EDITING_TASK_KEY, None)
        context.user_data.pop(EDITING_MODE_KEY, None)
        parking = await asyncio.to_thread(get_active_parking, user_id)
        if parking:
            await query.edit_message_text(_parking_message(parking, local_timezone), reply_markup=_parking_buttons(parking))
            return
        context.user_data[LOCATION_MODE_KEY] = "parking"
        await query.edit_message_text("🚘 Log parking\n\nShare your current location while you are beside the car.")
        await query.message.reply_text("Parking location", reply_markup=_location_keyboard("🚘 Save parking location"))
        return

    if data == "parking:bay":
        context.user_data[PARKING_BAY_MODE_KEY] = True
        await query.edit_message_text(
            "🅿️ Parking bay\n\nSend the bay, row, floor, or zone number.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Back", callback_data="parking")]]),
        )
        return

    if data == "parking:skip_bay":
        context.user_data.pop(PARKING_BAY_MODE_KEY, None)
        parking = await asyncio.to_thread(get_active_parking, user_id)
        if parking:
            await query.edit_message_text(_parking_message(parking, local_timezone), reply_markup=_parking_buttons(parking))
        else:
            message, buttons = await _home_payload(user_id)
            await query.edit_message_text(message, reply_markup=buttons)
        return

    if data == "parking:clear":
        context.user_data.pop(PARKING_BAY_MODE_KEY, None)
        parking = await asyncio.to_thread(clear_parking_location, user_id)
        if parking:
            message, buttons = await _home_payload(user_id)
            await query.edit_message_text("✅ Parking cleared", reply_markup=buttons)
            await query.message.reply_text(message, reply_markup=buttons)
        else:
            await query.edit_message_text("No parked car is currently logged.")
        return

    if data == "tasks:pending":
        context.user_data.pop(EDITING_TASK_KEY, None)
        context.user_data.pop(EDITING_MODE_KEY, None)
        tasks = await asyncio.to_thread(list_pending_tasks, user_id)
        await query.edit_message_text(
            _task_list_message(tasks, "Current Tasks", "📋 No current tasks.", local_timezone),
            reply_markup=_task_list_buttons(tasks) if tasks else None,
        )
        return

    if data == "tasks:today":
        context.user_data.pop(EDITING_TASK_KEY, None)
        context.user_data.pop(EDITING_MODE_KEY, None)
        tasks = await asyncio.to_thread(list_today_tasks, user_id)
        await query.edit_message_text(
            _task_list_message(tasks, "Due Today", "📅 Nothing due today.", local_timezone),
            reply_markup=_task_list_buttons(tasks) if tasks else None,
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
            await query.edit_message_text(f"✅ Done\n\n{task['title']}", reply_markup=_undo_buttons(task["id"]))
        else:
            await query.edit_message_text("That task is already done or no longer available.")
        return

    if action == "undo" and task_id:
        task = await asyncio.to_thread(restore_task_by_id, user_id, task_id)
        if task:
            await query.edit_message_text(_task_card(task, local_timezone, "Restored", "↩️"), reply_markup=_task_buttons(task))
        else:
            await query.edit_message_text("That task could not be restored.")
        return

    if action == "snooze20" and task_id:
        task = await asyncio.to_thread(snooze_task_by_id, user_id, task_id, 20)
        if task:
            await query.edit_message_text(_task_card(task, local_timezone, "Remind again", "⏰"), reply_markup=_task_buttons(task))
        else:
            await query.edit_message_text("That task is already done or no longer available.")


async def on_startup(application: Application) -> None:
    global REMINDER_STOP_EVENT, REMINDER_TASK
    await asyncio.to_thread(ensure_schema)
    await application.bot.set_my_commands(
        [
            BotCommand("home", "Open Atlas Life OS"),
            BotCommand("tasks", "View current tasks"),
            BotCommand("today", "View tasks due today"),
            BotCommand("help", "Show commands"),
        ]
    )
    mini_url = _mini_app_url()
    if mini_url:
        await application.bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Atlas", web_app=WebAppInfo(url=f"{mini_url}/app")))
    logger.info("Database schema and Telegram command menu are ready")
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
    application.add_handler(CommandHandler("home", start))
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
