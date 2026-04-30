from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel


load_dotenv()


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    database_url: str
    openai_api_key: str | None
    whisper_model_size: str
    whisper_language: str | None
    reminder_check_seconds: int
    local_timezone: str
    mini_app_url: str | None
    port: int


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_config() -> Config:
    return Config(
        telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
        database_url=_required("DATABASE_URL"),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        whisper_model_size=os.getenv("WHISPER_MODEL_SIZE", "small"),
        whisper_language=os.getenv("WHISPER_LANGUAGE", "en") or None,
        reminder_check_seconds=int(os.getenv("REMINDER_CHECK_SECONDS", "60")),
        local_timezone=os.getenv("LOCAL_TIMEZONE", "Asia/Dubai"),
        mini_app_url=os.getenv("MINI_APP_URL") or None,
        port=int(os.getenv("PORT", "8000")),
    )


config = load_config()
web_app = FastAPI(title="Atlas Life OS Mini App")


class UserContext(BaseModel):
    telegram_user_id: str
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None


class CaptureIn(BaseModel):
    text: str


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


def _validate_init_data(init_data: str) -> UserContext:
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Open Atlas from Telegram to sign in")
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
    from db import ensure_user_access

    user = _validate_init_data(x_telegram_init_data)
    allowed = ensure_user_access(user.telegram_user_id, user.username, user.first_name, user.last_name)
    if not allowed:
        raise HTTPException(status_code=403, detail="Atlas Life OS access is paused")
    return user


def _task_payload(task: dict[str, Any], local_timezone: str) -> dict[str, Any]:
    return {
        "id": task["id"],
        "title": task["title"],
        "due_label": _format_dt(task.get("due_at"), local_timezone) or "No due time set",
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


async def _create_mini_task(user: UserContext, text: str, source_type: str, transcribed_text: str | None = None) -> dict[str, Any]:
    from db import create_task, get_user_timezone
    from parser import parse_task

    cleaned = text.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Add a task first")
    local_timezone = get_user_timezone(user.telegram_user_id)
    parsed = await parse_task(cleaned, local_timezone)
    return create_task(
        {
            "telegram_user_id": user.telegram_user_id,
            "telegram_chat_id": user.telegram_user_id,
            "source_type": source_type,
            "raw_input": "" if source_type == "voice" else cleaned,
            "transcribed_text": transcribed_text,
            "title": parsed.title,
            "due_at": parsed.due_at,
            "category": parsed.category,
            "priority": parsed.priority,
        }
    )


MINI_APP_HTML = """
<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Atlas Life OS</title><script src="https://telegram.org/js/telegram-web-app.js"></script><style>
:root{color-scheme:dark;--bg:#101418;--panel:#171d23;--panel2:#1f2830;--line:#303a44;--text:#f5f1e8;--muted:#a9b3bc;--gold:#d6b66d;--green:#67d391;--red:#ff7a7a}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif}.shell{width:min(720px,100%);margin:0 auto;padding:18px 12px 28px}h1{font-size:25px;margin:0 0 4px}.muted{color:var(--muted);font-size:13px;line-height:1.4}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:14px 0}.metric,.section,.task,.parking{background:var(--panel);border:1px solid var(--line);border-radius:8px}.metric{padding:12px}.metric strong{display:block;color:var(--gold);font-size:24px}.section{padding:14px;margin-top:10px}h2{font-size:15px;margin:0 0 10px}.task,.parking{background:var(--panel2);padding:12px;margin-top:8px}.title{font-size:15px;margin-bottom:8px}.meta{color:var(--muted);font-size:12px;margin-bottom:10px}.actions{display:grid;grid-template-columns:1fr 1fr;gap:8px}.button{border:1px solid var(--line);border-radius:8px;min-height:42px;background:#25303a;color:var(--text);padding:10px 12px;text-decoration:none;display:flex;align-items:center;justify-content:center;text-align:center}.primary{background:var(--gold);border-color:var(--gold);color:#16130b;font-weight:700}.success{background:rgba(103,211,145,.14);border-color:rgba(103,211,145,.55)}.danger{background:rgba(255,122,122,.12);border-color:rgba(255,122,122,.48)}input,textarea{width:100%;border-radius:8px;border:1px solid var(--line);background:#101820;color:var(--text);padding:11px 12px;font:inherit}textarea{min-height:86px;resize:vertical}.capture-actions{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}.recording{border-color:var(--red);color:var(--red)}.toast{position:fixed;left:12px;right:12px;bottom:12px;padding:12px;background:#0d1115;border:1px solid var(--line);border-radius:8px;display:none}@media(max-width:420px){.grid,.actions,.capture-actions{grid-template-columns:1fr}}
</style></head><body><main class="shell"><h1>Atlas Life OS</h1><div id="tz" class="muted">Loading workspace</div><section class="grid"><div class="metric"><strong id="pending">0</strong><span class="muted">Tasks</span></div><div class="metric"><strong id="today">0</strong><span class="muted">Today</span></div><div class="metric"><strong id="parkingState">-</strong><span class="muted">Parking</span></div></section><section class="section"><h2>Capture</h2><textarea id="captureText" placeholder="Type a task, for example: Chase invoice tomorrow at 9am"></textarea><div class="capture-actions"><button id="saveTask" class="button primary" type="button">Save task</button><button id="recordTask" class="button" type="button">Record voice</button></div><div id="recordHint" class="muted" style="margin-top:8px">Capture here first. Telegram chat still works as a backup.</div></section><section class="section"><h2>Parking</h2><div id="parking" class="parking"></div></section><section class="section"><h2>Current Tasks</h2><button id="refresh" class="button" type="button">Refresh</button><div id="tasks"></div></section></main><div id="toast" class="toast"></div><script>
const tg=window.Telegram?.WebApp;const initData=tg?.initData||"";let recorder=null,chunks=[],recording=false;if(tg){tg.ready();tg.expand();tg.setHeaderColor("#101418");tg.setBackgroundColor("#101418")}const $=id=>document.getElementById(id);function toast(m){$("toast").textContent=m;$("toast").style.display="block";clearTimeout(toast.t);toast.t=setTimeout(()=>$("toast").style.display="none",3000)}async function api(path,opt={}){const res=await fetch(path,{...opt,headers:{"Content-Type":"application/json","X-Telegram-Init-Data":initData,...(opt.headers||{})}});if(!res.ok){const b=await res.json().catch(()=>({}));throw Error(b.detail||"Request failed")}return res.json()}async function upload(path,fd){const res=await fetch(path,{method:"POST",headers:{"X-Telegram-Init-Data":initData},body:fd});if(!res.ok){const b=await res.json().catch(()=>({}));throw Error(b.detail||"Upload failed")}return res.json()}async function mutate(fn,msg){try{await fn();toast(msg);await load()}catch(e){toast(e.message)}}function renderParking(p){const el=$("parking");if(!p){el.innerHTML='<div class="muted">No parking location saved.</div><button id="saveParking" class="button primary" type="button">Save current location</button>';$("saveParking").onclick=saveParking;return}el.innerHTML=`<div class="meta">${p.updated_label||"Saved"}<br>${p.bay_number?"Bay "+p.bay_number:"No bay added"}</div><div class="actions"><a class="button primary" href="${p.maps_url}" target="_blank">Directions</a><button id="clearParking" class="button danger" type="button">Picked up</button></div><input id="bayInput" placeholder="Bay, row, floor" value="${p.bay_number||""}" style="margin-top:8px"><button id="saveBay" class="button" type="button" style="margin-top:8px;width:100%">Save bay</button>`;$("clearParking").onclick=()=>mutate(()=>api("/api/parking",{method:"DELETE"}),"Parking cleared");$("saveBay").onclick=()=>{const v=$("bayInput").value.trim();if(v)mutate(()=>api("/api/parking/bay",{method:"POST",body:JSON.stringify({bay_number:v})}),"Bay saved")}}function renderTask(t){const el=document.createElement("div");el.className="task";el.innerHTML=`<div class="title"></div><div class="meta"></div><div class="actions"><button class="button success">Done</button><button class="button">20 min</button></div>`;el.querySelector(".title").textContent=t.title;el.querySelector(".meta").textContent=t.due_label;el.querySelectorAll("button")[0].onclick=()=>mutate(()=>api(`/api/tasks/${t.id}/done`,{method:"POST"}),"Task done");el.querySelectorAll("button")[1].onclick=()=>mutate(()=>api(`/api/tasks/${t.id}/snooze`,{method:"POST",body:JSON.stringify({minutes:20})}),"Moved 20 minutes");return el}function render(d){$("tz").textContent=d.timezone;$("pending").textContent=d.counts.pending;$("today").textContent=d.counts.today;$("parkingState").textContent=d.parking?"Saved":"Open";renderParking(d.parking);const tasks=$("tasks");tasks.innerHTML="";if(!d.tasks.length){tasks.innerHTML='<div class="muted" style="padding-top:12px">No current tasks.</div>';return}d.tasks.forEach(t=>tasks.appendChild(renderTask(t)))}async function load(){if(!initData){toast("Open this from Telegram to sign in");return}try{render(await api("/api/me"))}catch(e){toast(e.message)}}function saveParking(){if(!navigator.geolocation){toast("Location unavailable");return}navigator.geolocation.getCurrentPosition(pos=>{const bay=prompt("Bay, row, or floor?","")||"";mutate(()=>api("/api/parking",{method:"POST",body:JSON.stringify({latitude:pos.coords.latitude,longitude:pos.coords.longitude,bay_number:bay.trim()||null})}),"Parking saved")},()=>toast("Location permission was not granted"),{enableHighAccuracy:true,timeout:12000})}async function saveTask(){const text=$("captureText").value.trim();if(!text){toast("Type a task first");return}await mutate(()=>api("/api/capture",{method:"POST",body:JSON.stringify({text})}),"Task saved");$("captureText").value=""}async function toggleRecord(){if(recording){recorder.stop();return}if(!navigator.mediaDevices?.getUserMedia){toast("Voice recording is not available here");return}try{const stream=await navigator.mediaDevices.getUserMedia({audio:true});chunks=[];recorder=new MediaRecorder(stream);recorder.ondataavailable=e=>{if(e.data.size)chunks.push(e.data)};recorder.onstop=async()=>{stream.getTracks().forEach(t=>t.stop());recording=false;$("recordTask").textContent="Record voice";$("recordTask").classList.remove("recording");const blob=new Blob(chunks,{type:recorder.mimeType||"audio/webm"});const fd=new FormData();fd.append("file",blob,"atlas-voice.webm");await mutate(()=>upload("/api/capture/voice",fd),"Voice task saved")};recording=true;$("recordTask").textContent="Stop recording";$("recordTask").classList.add("recording");recorder.start()}catch(e){toast("Microphone permission was not granted")}}$("refresh").onclick=load;$("saveTask").onclick=saveTask;$("recordTask").onclick=toggleRecord;load();
</script></body></html>
"""


@web_app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@web_app.get("/app", response_class=HTMLResponse)
def mini_app_home() -> str:
    return MINI_APP_HTML


@web_app.get("/api/me")
def api_me(user: UserContext = Depends(current_user)) -> dict[str, Any]:
    from db import get_active_parking, get_user_timezone, list_pending_tasks, list_today_tasks

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


@web_app.post("/api/capture")
async def api_capture(payload: CaptureIn, user: UserContext = Depends(current_user)) -> dict[str, Any]:
    local_timezone = _get_user_timezone(user.telegram_user_id)
    task = await _create_mini_task(user, payload.text, "text")
    return {"task": _task_payload(task, local_timezone)}


@web_app.post("/api/capture/voice")
async def api_capture_voice(file: UploadFile = File(...), user: UserContext = Depends(current_user)) -> dict[str, Any]:
    from voice import VoiceTranscriptionError, transcribe_audio_path

    suffix = Path(file.filename or "voice.webm").suffix or ".webm"
    source_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp_file:
            source_path = Path(temp_file.name)
            temp_file.write(await file.read())
        transcription = await transcribe_audio_path(source_path)
        local_timezone = _get_user_timezone(user.telegram_user_id)
        task = await _create_mini_task(user, transcription, "voice", transcription)
        return {"task": _task_payload(task, local_timezone), "transcription": transcription}
    except VoiceTranscriptionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if source_path:
            try:
                os.remove(source_path)
            except FileNotFoundError:
                pass


def _get_user_timezone(user_id: str) -> str:
    from db import get_user_timezone

    return get_user_timezone(user_id)


@web_app.post("/api/tasks/{task_id}/done")
def api_done_task(task_id: str, user: UserContext = Depends(current_user)) -> dict[str, Any]:
    from db import complete_task_by_id

    if not complete_task_by_id(user.telegram_user_id, task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    return {"ok": True}


@web_app.post("/api/tasks/{task_id}/snooze")
def api_snooze_task(task_id: str, payload: SnoozeIn, user: UserContext = Depends(current_user)) -> dict[str, Any]:
    from db import snooze_task_by_id

    minutes = max(1, min(payload.minutes, 1440))
    if not snooze_task_by_id(user.telegram_user_id, task_id, minutes):
        raise HTTPException(status_code=404, detail="Task not found")
    return {"ok": True}


@web_app.post("/api/parking")
def api_save_parking(payload: ParkingLocationIn, user: UserContext = Depends(current_user)) -> dict[str, Any]:
    from timezonefinder import TimezoneFinder
    from db import get_user_timezone, save_parking_location, set_parking_bay, set_user_timezone

    parking = save_parking_location(user.telegram_user_id, user.telegram_user_id, payload.latitude, payload.longitude)
    if payload.bay_number:
        parking = set_parking_bay(user.telegram_user_id, payload.bay_number) or parking
    timezone_name = TimezoneFinder().timezone_at(lat=payload.latitude, lng=payload.longitude)
    if timezone_name:
        set_user_timezone(user.telegram_user_id, timezone_name)
    return {"parking": _parking_payload(parking, get_user_timezone(user.telegram_user_id))}


@web_app.post("/api/parking/bay")
def api_set_parking_bay(payload: ParkingBayIn, user: UserContext = Depends(current_user)) -> dict[str, Any]:
    from db import get_user_timezone, set_parking_bay

    parking = set_parking_bay(user.telegram_user_id, payload.bay_number.strip())
    if not parking:
        raise HTTPException(status_code=404, detail="No parking location")
    return {"parking": _parking_payload(parking, get_user_timezone(user.telegram_user_id))}


@web_app.delete("/api/parking")
def api_clear_parking(user: UserContext = Depends(current_user)) -> dict[str, Any]:
    from db import clear_parking_location

    clear_parking_location(user.telegram_user_id)
    return {"ok": True}
