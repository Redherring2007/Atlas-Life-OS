from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from timezonefinder import TimezoneFinder

from config import config
from db import (
    clear_parking_location,
    complete_task_by_id,
    ensure_user_access,
    get_active_parking,
    get_user_timezone,
    list_pending_tasks,
    list_today_tasks,
    save_parking_location,
    set_parking_bay,
    set_user_timezone,
    snooze_task_by_id,
)


STATIC_DIR = Path(__file__).parent / "static"
TIMEZONE_FINDER = TimezoneFinder()
app = FastAPI(title="Atlas Life OS Mini App")


class UserContext(BaseModel):
    telegram_user_id: str
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None


class ParkingLocationIn(BaseModel):
    latitude: float
    longitude: float
    bay_number: str | None = None


class ParkingBayIn(BaseModel):
    bay_number: str


class SnoozeIn(BaseModel):
    minutes: int = 20


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


@app.post("/api/tasks/{task_id}/done")
def api_done_task(task_id: str, user: UserContext = Depends(current_user)) -> dict[str, Any]:
    task = complete_task_by_id(user.telegram_user_id, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"ok": True}


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
