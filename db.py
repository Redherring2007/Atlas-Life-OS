from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from config import config


TASK_ORDER = "due_at ASC NULLS LAST, created_at DESC"


def _connect() -> psycopg.Connection:
    return psycopg.connect(config.database_url, row_factory=dict_row)


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
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(hour=23, minute=59, second=59, microsecond=999999)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select *
                from tasks
                where telegram_user_id = %s
                  and status = 'pending'
                  and due_at >= %s
                  and due_at <= %s
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
    now = datetime.now(timezone.utc)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                update tasks
                set status = 'done', completed_at = %s
                where id = %s
                returning *
                """,
                (now, task["id"]),
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
                select *
                from tasks
                where status = 'pending'
                  and reminder_sent = false
                  and due_at <= %s
                order by {TASK_ORDER}
                """,
                (now,),
            )
            return _serialize_tasks(cur.fetchall())


def mark_reminder_sent(task_id: str) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("update tasks set reminder_sent = true where id = %s", (task_id,))
