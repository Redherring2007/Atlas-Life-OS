from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

from config import config


TASK_ORDER = "due_at ASC NULLS LAST, created_at DESC"

SCHEMA_SQL = """
create extension if not exists pgcrypto;

create table if not exists public.tasks (
    id uuid primary key default gen_random_uuid(),
    telegram_user_id text not null,
    telegram_chat_id text not null,
    source_type text not null check (source_type in ('text', 'voice')),
    raw_input text,
    transcribed_text text,
    title text not null,
    due_at timestamptz null,
    category text not null,
    priority text not null,
    status text default 'pending',
    reminder_sent boolean default false,
    created_at timestamptz default now(),
    updated_at timestamptz default now(),
    completed_at timestamptz null
);

create table if not exists public.user_settings (
    telegram_user_id text primary key,
    timezone text not null,
    updated_at timestamptz default now()
);

create table if not exists public.parking_locations (
    telegram_user_id text primary key,
    telegram_chat_id text not null,
    latitude double precision not null,
    longitude double precision not null,
    bay_number text null,
    active boolean default true,
    created_at timestamptz default now(),
    updated_at timestamptz default now(),
    cleared_at timestamptz null
);

create or replace function public.set_updated_at()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

drop trigger if exists tasks_set_updated_at on public.tasks;
create trigger tasks_set_updated_at
before update on public.tasks
for each row
execute function public.set_updated_at();

drop trigger if exists user_settings_set_updated_at on public.user_settings;
create trigger user_settings_set_updated_at
before update on public.user_settings
for each row
execute function public.set_updated_at();

drop trigger if exists parking_locations_set_updated_at on public.parking_locations;
create trigger parking_locations_set_updated_at
before update on public.parking_locations
for each row
execute function public.set_updated_at();

create index if not exists tasks_telegram_user_status_idx on public.tasks (telegram_user_id, status);
create index if not exists tasks_due_at_idx on public.tasks (due_at);
create index if not exists tasks_reminder_sent_status_idx on public.tasks (reminder_sent, status);
create index if not exists tasks_created_at_idx on public.tasks (created_at);
create index if not exists parking_locations_active_idx on public.parking_locations (telegram_user_id, active);
"""


def _connect() -> psycopg.Connection:
    return psycopg.connect(config.database_url, row_factory=dict_row)


def ensure_schema() -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)


def _serialize_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return value


def _serialize_task(task: dict[str, Any] | None) -> dict[str, Any] | None:
    if task is None:
        return None
    return {key: _serialize_value(value) for key, value in task.items()}


def _serialize_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_serialize_task(task) for task in tasks if task is not None]


def _serialize_parking(parking: dict[str, Any] | None) -> dict[str, Any] | None:
    if parking is None:
        return None
    return {key: _serialize_value(value) for key, value in parking.items()}


def get_user_timezone(telegram_user_id: str) -> str:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("select timezone from user_settings where telegram_user_id = %s", (str(telegram_user_id),))
            row = cur.fetchone()
            return row["timezone"] if row else config.local_timezone


def set_user_timezone(telegram_user_id: str, timezone_name: str) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into user_settings (telegram_user_id, timezone)
                values (%s, %s)
                on conflict (telegram_user_id)
                do update set timezone = excluded.timezone
                """,
                (str(telegram_user_id), timezone_name),
            )


def get_active_parking(telegram_user_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select *
                from parking_locations
                where telegram_user_id = %s and active = true
                """,
                (str(telegram_user_id),),
            )
            return _serialize_parking(cur.fetchone())


def save_parking_location(telegram_user_id: str, telegram_chat_id: str, latitude: float, longitude: float) -> dict[str, Any]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into parking_locations (
                    telegram_user_id,
                    telegram_chat_id,
                    latitude,
                    longitude,
                    bay_number,
                    active,
                    cleared_at
                )
                values (%s, %s, %s, %s, null, true, null)
                on conflict (telegram_user_id)
                do update set telegram_chat_id = excluded.telegram_chat_id,
                              latitude = excluded.latitude,
                              longitude = excluded.longitude,
                              bay_number = null,
                              active = true,
                              cleared_at = null
                returning *
                """,
                (str(telegram_user_id), str(telegram_chat_id), latitude, longitude),
            )
            return _serialize_parking(cur.fetchone())


def set_parking_bay(telegram_user_id: str, bay_number: str) -> dict[str, Any] | None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                update parking_locations
                set bay_number = %s
                where telegram_user_id = %s and active = true
                returning *
                """,
                (bay_number[:80], str(telegram_user_id)),
            )
            return _serialize_parking(cur.fetchone())


def clear_parking_location(telegram_user_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                update parking_locations
                set active = false, cleared_at = now()
                where telegram_user_id = %s and active = true
                returning *
                """,
                (str(telegram_user_id),),
            )
            return _serialize_parking(cur.fetchone())


def create_task(task: dict[str, Any]) -> dict[str, Any]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into tasks (
                    telegram_user_id,
                    telegram_chat_id,
                    source_type,
                    raw_input,
                    transcribed_text,
                    title,
                    due_at,
                    category,
                    priority
                )
                values (
                    %(telegram_user_id)s,
                    %(telegram_chat_id)s,
                    %(source_type)s,
                    %(raw_input)s,
                    %(transcribed_text)s,
                    %(title)s,
                    %(due_at)s,
                    %(category)s,
                    %(priority)s
                )
                returning *
                """,
                task,
            )
            return _serialize_task(cur.fetchone())


def get_task_by_id(telegram_user_id: str, task_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select *
                from tasks
                where id = %s and telegram_user_id = %s and status = 'pending'
                """,
                (task_id, str(telegram_user_id)),
            )
            return _serialize_task(cur.fetchone())


def update_task_by_id(telegram_user_id: str, task_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                update tasks
                set raw_input = %(raw_input)s,
                    transcribed_text = %(transcribed_text)s,
                    title = %(title)s,
                    due_at = %(due_at)s,
                    category = %(category)s,
                    priority = %(priority)s,
                    reminder_sent = false
                where id = %(id)s and telegram_user_id = %(telegram_user_id)s and status = 'pending'
                returning *
                """,
                {
                    "id": task_id,
                    "telegram_user_id": str(telegram_user_id),
                    **updates,
                },
            )
            return _serialize_task(cur.fetchone())


def update_task_fields_by_id(telegram_user_id: str, task_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    assignments = []
    values: dict[str, Any] = {"id": task_id, "telegram_user_id": str(telegram_user_id)}
    for key, value in fields.items():
        assignments.append(f"{key} = %({key})s")
        values[key] = value
    if "due_at" in fields:
        assignments.append("reminder_sent = false")
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                update tasks
                set {", ".join(assignments)}
                where id = %(id)s and telegram_user_id = %(telegram_user_id)s and status = 'pending'
                returning *
                """,
                values,
            )
            return _serialize_task(cur.fetchone())


def list_pending_tasks(telegram_user_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select *
                from tasks
                where telegram_user_id = %s and status = 'pending'
                order by {TASK_ORDER}
                """,
                (str(telegram_user_id),),
            )
            return _serialize_tasks(cur.fetchall())


def list_today_tasks(telegram_user_id: str) -> list[dict[str, Any]]:
    local_tz = ZoneInfo(get_user_timezone(telegram_user_id))
    now = datetime.now(local_tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    end = (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).astimezone(timezone.utc)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select *
                from tasks
                where telegram_user_id = %s
                  and status = 'pending'
                  and due_at >= %s
                  and due_at < %s
                order by {TASK_ORDER}
                """,
                (str(telegram_user_id), start, end),
            )
            return _serialize_tasks(cur.fetchall())


def list_overdue_tasks(telegram_user_id: str) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select *
                from tasks
                where telegram_user_id = %s
                  and status = 'pending'
                  and due_at < %s
                order by {TASK_ORDER}
                """,
                (str(telegram_user_id), now),
            )
            return _serialize_tasks(cur.fetchall())


def _task_by_number(telegram_user_id: str, task_number: int) -> dict[str, Any] | None:
    if task_number < 1:
        return None
    tasks = list_pending_tasks(telegram_user_id)
    if task_number > len(tasks):
        return None
    return tasks[task_number - 1]


def complete_task_by_number(telegram_user_id: str, task_number: int) -> dict[str, Any] | None:
    task = _task_by_number(telegram_user_id, task_number)
    if not task:
        return None
    return complete_task_by_id(telegram_user_id, task["id"])


def complete_task_by_id(telegram_user_id: str, task_id: str) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                update tasks
                set status = 'done', completed_at = %s
                where id = %s and telegram_user_id = %s and status = 'pending'
                returning *
                """,
                (now, task_id, str(telegram_user_id)),
            )
            return _serialize_task(cur.fetchone())


def restore_task_by_id(telegram_user_id: str, task_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                update tasks
                set status = 'pending', completed_at = null
                where id = %s and telegram_user_id = %s and status = 'done'
                returning *
                """,
                (task_id, str(telegram_user_id)),
            )
            return _serialize_task(cur.fetchone())


def snooze_task_by_id(telegram_user_id: str, task_id: str, minutes: int = 20) -> dict[str, Any] | None:
    due_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                update tasks
                set due_at = %s, reminder_sent = false
                where id = %s and telegram_user_id = %s and status = 'pending'
                returning *
                """,
                (due_at, task_id, str(telegram_user_id)),
            )
            return _serialize_task(cur.fetchone())


def delete_task_by_number(telegram_user_id: str, task_number: int) -> dict[str, Any] | None:
    task = _task_by_number(telegram_user_id, task_number)
    if not task:
        return None
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("delete from tasks where id = %s returning *", (task["id"],))
            deleted = _serialize_task(cur.fetchone())
            return deleted or task


def fetch_due_reminder_tasks() -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select tasks.*, coalesce(user_settings.timezone, %s) as user_timezone
                from tasks
                left join user_settings on user_settings.telegram_user_id = tasks.telegram_user_id
                where status = 'pending'
                  and reminder_sent = false
                  and due_at <= %s
                order by {TASK_ORDER}
                """,
                (config.local_timezone, now),
            )
            return _serialize_tasks(cur.fetchall())


def mark_reminder_sent(task_id: str) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("update tasks set reminder_sent = true where id = %s", (task_id,))
