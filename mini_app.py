from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from timezonefinder import TimezoneFinder

from config import config
from db import (
    clear_parking_location,
    complete_task_by_id,
    create_task,
    ensure_user_access,
    get_active_parking,
    get_user_timezone,
    list_pending_tasks,
    list_today_tasks,
    restore_task_by_id,
    save_parking_location,
    set_parking_bay,
    set_task_reminder_offset,
    set_user_timezone,
    snooze_task_by_id,
    update_task_by_id,
)
from parser import parse_task


STATIC_DIR = Path(__file__).parent / "static"
TIMEZONE_FINDER = TimezoneFinder()
app = FastAPI(title="Atlas Life OS Mini App")


class UserContext(BaseModel):
    telegram_user_id: str
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None


class CaptureIn(BaseModel):
    text: str


class TaskEditIn(BaseModel):
    text: str


class ParkingLocationIn(BaseModel):
    latitude: float
    longitude: float
    bay_number: str | None = None


class ParkingBayIn(BaseModel):
    bay_number: str


class SnoozeIn(BaseModel):
    minutes: int = 20


class ReminderBeforeIn(BaseModel):
    minutes: int


def _format_dt(value: str | None, local_timezone: str) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ZoneInfo(local_timezone))
        return dt.strftime("%a %d %b, %I:%M %p").lstrip("0")
    except ValueError:
        return value


def _task_payload(task: dict[str, Any], local_timezone: str) -> dict[str, Any]:
    return {
        "id": task["id"],
        "title": task["title"],
        "due_at": task.get("due_at"),
        "due_label": _format_dt(task.get("due_at"), local_timezone) or "No due time set",
        "status": task.get("status"),
    }


def _parking_payload(parking: dict[str, Any] | None, local_timezone: str) -> dict[str, Any] | None:
    if not parking:
        return None
    return {
        "latitude": parking["latitude"],
        "longitude": parking["longitude"],
        "bay_number": parking.get("bay_number"),
        "updated_label": _format_dt(parking.get("updated_at") or parking.get("created_at"), local_timezone),
        "maps_url": f"https://www.google.com/maps/search/?api=1&query={parking['latitude']},{parking['longitude']}",
    }


async def _parse_task_text(text: str, local_timezone: str) -> dict[str, Any]:
    cleaned = text.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Add a task first")
    parsed = await parse_task(cleaned, local_timezone)
    return {
        "raw_input": cleaned,
        "transcribed_text": None,
        "title": parsed.title,
        "due_at": parsed.due_at,
        "category": parsed.category,
        "priority": parsed.priority,
    }


def _validate_init_data(init_data: str) -> UserContext:
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Missing Telegram signature")

    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", config.telegram_bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid Telegram signature")

    try:
        user = json.loads(values.get("user") or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=401, detail="Invalid Telegram user") from exc
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Missing Telegram user")
    return UserContext(
        telegram_user_id=str(user_id),
        username=user.get("username"),
        first_name=user.get("first_name"),
        last_name=user.get("last_name"),
    )


def current_user(x_telegram_init_data: str = Header(default="")) -> UserContext:
    user = _validate_init_data(x_telegram_init_data)
    allowed = ensure_user_access(user.telegram_user_id, user.username, user.first_name, user.last_name)
    if not allowed:
        raise HTTPException(status_code=403, detail="Atlas Life OS access is paused")
    return user


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/app", response_class=HTMLResponse)
def mini_app_home() -> str:
    return (STATIC_DIR / "mini_app.html").read_text(encoding="utf-8")


@app.get("/api/me")
def api_me(user: UserContext = Depends(current_user)) -> dict[str, Any]:
    local_timezone = get_user_timezone(user.telegram_user_id)
    pending = list_pending_tasks(user.telegram_user_id)
    today = list_today_tasks(user.telegram_user_id)
    parking = get_active_parking(user.telegram_user_id)
    return {
        "user": user.model_dump(),
        "timezone": local_timezone,
        "counts": {"pending": len(pending), "today": len(today)},
        "tasks": [_task_payload(task, local_timezone) for task in pending[:20]],
        "today": [_task_payload(task, local_timezone) for task in today[:20]],
        "parking": _parking_payload(parking, local_timezone),
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/capture")
async def api_capture(payload: CaptureIn, user: UserContext = Depends(current_user)) -> dict[str, Any]:
    local_timezone = get_user_timezone(user.telegram_user_id)
    updates = await _parse_task_text(payload.text, local_timezone)
    task = create_task(
        {
            "telegram_user_id": user.telegram_user_id,
            "telegram_chat_id": user.telegram_user_id,
            "source_type": "text",
            **updates,
        }
    )
    return {"task": _task_payload(task, local_timezone)}


@app.post("/api/capture/voice")
async def api_capture_voice(file: UploadFile = File(...), user: UserContext = Depends(current_user)) -> dict[str, Any]:
    from voice import VoiceTranscriptionError, transcribe_audio_path

    suffix = Path(file.filename or "voice.webm").suffix or ".webm"
    source_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp_file:
            source_path = Path(temp_file.name)
            temp_file.write(await file.read())
        transcription = await transcribe_audio_path(source_path)
        local_timezone = get_user_timezone(user.telegram_user_id)
        parsed = await _parse_task_text(transcription, local_timezone)
        task = create_task(
            {
                "telegram_user_id": user.telegram_user_id,
                "telegram_chat_id": user.telegram_user_id,
                "source_type": "voice",
                **parsed,
                "raw_input": "",
                "transcribed_text": transcription,
            }
        )
        return {"task": _task_payload(task, local_timezone), "transcription": transcription}
    except VoiceTranscriptionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if source_path:
            try:
                os.remove(source_path)
            except FileNotFoundError:
                pass


@app.post("/api/tasks/{task_id}/done")
def api_done_task(task_id: str, user: UserContext = Depends(current_user)) -> dict[str, Any]:
    task = complete_task_by_id(user.telegram_user_id, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    local_timezone = get_user_timezone(user.telegram_user_id)
    return {"task": _task_payload(task, local_timezone)}


@app.post("/api/tasks/{task_id}/undo")
def api_undo_task(task_id: str, user: UserContext = Depends(current_user)) -> dict[str, Any]:
    task = restore_task_by_id(user.telegram_user_id, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task could not be restored")
    local_timezone = get_user_timezone(user.telegram_user_id)
    return {"task": _task_payload(task, local_timezone)}


@app.put("/api/tasks/{task_id}")
async def api_edit_task(task_id: str, payload: TaskEditIn, user: UserContext = Depends(current_user)) -> dict[str, Any]:
    local_timezone = get_user_timezone(user.telegram_user_id)
    updates = await _parse_task_text(payload.text, local_timezone)
    task = update_task_by_id(user.telegram_user_id, task_id, updates)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task": _task_payload(task, local_timezone)}


@app.post("/api/tasks/{task_id}/reminder-before")
def api_set_reminder_before(task_id: str, payload: ReminderBeforeIn, user: UserContext = Depends(current_user)) -> dict[str, Any]:
    minutes = max(0, min(payload.minutes, 1440))
    task = set_task_reminder_offset(user.telegram_user_id, task_id, minutes)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    local_timezone = get_user_timezone(user.telegram_user_id)
    return {"task": _task_payload(task, local_timezone)}


@app.post("/api/tasks/{task_id}/snooze")
def api_snooze_task(task_id: str, payload: SnoozeIn, user: UserContext = Depends(current_user)) -> dict[str, Any]:
    minutes = max(1, min(payload.minutes, 1440))
    task = snooze_task_by_id(user.telegram_user_id, task_id, minutes)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"ok": True}


@app.post("/api/parking")
def api_save_parking(payload: ParkingLocationIn, user: UserContext = Depends(current_user)) -> dict[str, Any]:
    parking = save_parking_location(user.telegram_user_id, user.telegram_user_id, payload.latitude, payload.longitude)
    if payload.bay_number:
        parking = set_parking_bay(user.telegram_user_id, payload.bay_number) or parking
    timezone_name = TIMEZONE_FINDER.timezone_at(lat=payload.latitude, lng=payload.longitude)
    if timezone_name:
        set_user_timezone(user.telegram_user_id, timezone_name)
    local_timezone = get_user_timezone(user.telegram_user_id)
    return {"parking": _parking_payload(parking, local_timezone)}


@app.post("/api/parking/bay")
def api_set_parking_bay(payload: ParkingBayIn, user: UserContext = Depends(current_user)) -> dict[str, Any]:
    parking = set_parking_bay(user.telegram_user_id, payload.bay_number.strip())
    if not parking:
        raise HTTPException(status_code=404, detail="No parking location")
    local_timezone = get_user_timezone(user.telegram_user_id)
    return {"parking": _parking_payload(parking, local_timezone)}


@app.delete("/api/parking")
def api_clear_parking(user: UserContext = Depends(current_user)) -> dict[str, Any]:
    clear_parking_location(user.telegram_user_id)
    return {"ok": True}
