from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db import execute, open_db
from app.feishu import TenantApp, send_card, send_message
from app.feishu_cards import build_answer_card
from app.graph import run_agent
from app.settings import get_settings


DB_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_TIMEZONE = "Asia/Shanghai"
SHANGHAI_TIMEZONE = timezone(timedelta(hours=8), name=DEFAULT_TIMEZONE)
RETRY_DELAYS_SECONDS = (30, 60, 300, 900, 3600)
MAX_DELIVERY_CHARS = 18_000
logger = logging.getLogger("feishu_scheduler")


class PermanentTaskError(RuntimeError):
    pass


async def run_scheduler(apps: list[TenantApp]) -> None:
    app_by_id = {app.app_id: app for app in apps}
    settings = get_settings()
    last_email_cleanup = 0.0
    while True:
        try:
            await run_due_tasks(app_by_id)
            if settings.email_feature_enabled and time.monotonic() - last_email_cleanup >= 3600:
                from app.email_repository import cleanup_expired_email_data

                last_email_cleanup = time.monotonic()
                await cleanup_expired_email_data()
        except Exception:
            logger.exception("Scheduled task polling failed")
        await asyncio.sleep(max(1, settings.scheduler_poll_seconds))


async def run_due_tasks(app_by_id: dict[str, TenantApp]) -> None:
    tasks = await claim_due_tasks(now_text())
    semaphore = asyncio.Semaphore(max(1, get_settings().scheduler_concurrency))

    async def guarded(task: dict[str, Any]) -> None:
        async with semaphore:
            app = app_by_id.get(task["app_id"])
            if not app:
                run_id = uuid.uuid4().hex
                attempt = int(task.get("consecutive_failures") or 0) + 1
                await start_task_run(task["id"], run_id, attempt)
                await handle_task_failure(
                    task, run_id, "对应的飞书应用未启用", permanent=True
                )
                return
            await execute_claimed_task(app, task)

    await asyncio.gather(*(guarded(task) for task in tasks))


async def execute_claimed_task(app: TenantApp, task: dict[str, Any]) -> None:
    run_id = uuid.uuid4().hex
    attempt = int(task.get("consecutive_failures") or 0) + 1
    await start_task_run(task["id"], run_id, attempt)
    try:
        output = await asyncio.wait_for(
            execute_task_payload(task, app, run_id),
            timeout=max(1, int(task.get("timeout_seconds") or 120)),
        )
        await deliver_task_result(app, task, output)
        await finalize_email_delivery(task, run_id, success=True)
    except PermanentTaskError as exc:
        await finalize_email_delivery(task, run_id, success=False, error=type(exc).__name__)
        logger.warning("Scheduled task permanently denied: task_id=%s", task["id"])
        await handle_task_failure(task, run_id, str(exc), permanent=True, app=app)
    except TimeoutError:
        await finalize_email_delivery(task, run_id, success=False, error="TimeoutError")
        error = f"执行超过 {task.get('timeout_seconds') or 120} 秒，已超时"
        logger.warning("Scheduled task timed out: task_id=%s", task["id"])
        await handle_task_failure(task, run_id, error, app=app)
    except Exception as exc:  # noqa: BLE001
        await finalize_email_delivery(task, run_id, success=False, error=type(exc).__name__)
        logger.exception("Scheduled task failed: task_id=%s", task["id"])
        await handle_task_failure(task, run_id, str(exc), app=app)
    else:
        await complete_task_run(run_id, _output_text(output))
        await mark_task_success(task, run_id)


async def execute_task_payload(
    task: dict[str, Any], app: TenantApp, run_id: str
) -> Any:
    if task.get("execution_mode") != "agent":
        return f"定时提醒：{task['prompt']}"

    prompt = re.sub(r"^定时(?:执行)?\s*", "", str(task["prompt"])).strip()
    event = {
        "tenant_key": task["tenant_key"],
        "app_id": task["app_id"],
        "bot_code": task.get("bot_code") or app.bot_code,
        "chat_id": task["chat_id"],
        "open_id": task["open_id"],
        "message_id": f"scheduled:{task['id']}:{run_id}",
        "text": prompt,
        "chat_type": task.get("chat_type") or "group",
        "_session_id": f"automation:{task['id']}:{run_id}",
        "_automation_run": True,
        "_email_task_id": task["id"],
    }
    from app.email_service import handle_email_command

    email_result = await handle_email_command(event)
    if email_result:
        if email_result.status == "denied":
            raise PermanentTaskError(email_result.answer)
        if email_result.status == "error":
            raise RuntimeError(email_result.answer)
        return email_result
    result = await run_agent(event)
    if result.status == "denied":
        raise PermanentTaskError(result.content)
    if result.status == "error":
        raise RuntimeError(result.content)
    return result.content


async def finalize_email_delivery(
    task: dict[str, Any], run_id: str, *, success: bool, error: str | None = None
) -> None:
    from app.email_repository import finalize_push_logs
    from app.email_service import looks_like_email_request

    if get_settings().email_feature_enabled and looks_like_email_request(task.get("prompt") or ""):
        try:
            await finalize_push_logs(
                f"scheduled:{task['id']}:{run_id}", success, error
            )
        except Exception:
            logger.exception("Failed to finalize scheduled email push log: task_id=%s", task["id"])


async def deliver_task_result(
    app: TenantApp, task: dict[str, Any], output: Any
) -> None:
    if task.get("execution_mode") == "agent":
        if getattr(output, "card", None):
            await send_card(app, task["chat_id"], output.card)
            return
        task_label = f"#{task['id']} {task.get('task_name') or '未命名任务'}"
        card = build_answer_card(
            f"{task_label}｜{task['prompt']}",
            _output_text(output)[:MAX_DELIVERY_CHARS],
            title="定时任务执行结果",
            footer_label="任务",
        )
        await send_card(app, task["chat_id"], card)
        return
    await send_message(
        app,
        task["chat_id"],
        msg_type="text",
        content={"text": _output_text(output)[:MAX_DELIVERY_CHARS]},
    )


def _output_text(output: Any) -> str:
    return str(getattr(output, "answer", output))


async def claim_due_tasks(now: str) -> list[dict[str, Any]]:
    settings = get_settings()
    lease_seconds = max(60, settings.scheduler_task_timeout_seconds + 60)
    locked_until = (now_datetime() + timedelta(seconds=lease_seconds)).strftime(
        DB_DATETIME_FORMAT
    )
    async with open_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            """
            UPDATE scheduled_task_run
            SET status = 'lost', finished_at = ?, error = '任务进程中断或执行租约已过期'
            WHERE status = 'running'
              AND task_id IN (
                SELECT id FROM scheduled_task
                WHERE locked_until IS NOT NULL AND locked_until <= ?
              )
            """,
            (now, now),
        )
        cur = await db.execute(
            """
            SELECT * FROM scheduled_task
            WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?
              AND (locked_until IS NULL OR locked_until <= ?)
            ORDER BY next_run_at, id
            LIMIT 20
            """,
            (now, now),
        )
        rows = [dict(row) for row in await cur.fetchall()]
        if rows:
            placeholders = ", ".join("?" for _ in rows)
            await db.execute(
                f"UPDATE scheduled_task SET locked_until = ?, last_status = 'running' "
                f"WHERE id IN ({placeholders})",
                (locked_until, *(row["id"] for row in rows)),
            )
        return rows


async def start_task_run(task_id: int, run_id: str, attempt: int) -> None:
    await execute(
        """
        INSERT INTO scheduled_task_run (run_id, task_id, status, attempt, started_at)
        VALUES (?, ?, 'running', ?, ?)
        """,
        (run_id, task_id, attempt, now_text()),
    )
    await execute(
        "UPDATE scheduled_task SET last_run_id = ? WHERE id = ?",
        (run_id, task_id),
    )


async def complete_task_run(run_id: str, output: str) -> None:
    await execute(
        """
        UPDATE scheduled_task_run
        SET status = 'success', finished_at = ?, output_summary = ?, error = NULL
        WHERE run_id = ?
        """,
        (now_text(), output[:5000], run_id),
    )


async def mark_task_success(task: dict[str, Any], run_id: str) -> None:
    now = now_text()
    if task["schedule_type"] in {"daily", "weekly", "weekly_multi", "interval"}:
        if task["schedule_type"] == "daily":
            next_run_at = next_daily_run(task["daily_time"])
        elif task["schedule_type"] == "weekly":
            next_run_at = next_weekly_run(int(task["weekly_day"]), task["daily_time"])
        elif task["schedule_type"] == "weekly_multi":
            next_run_at = next_weekly_days_run(str(task["weekly_day"]), task["daily_time"])
        else:
            next_run_at = next_interval_run(int(task["interval_minutes"]))
        await execute(
            """
            UPDATE scheduled_task
            SET last_run_at = ?, next_run_at = ?, locked_until = NULL,
                last_error = NULL, last_status = 'success', last_run_id = ?,
                consecutive_failures = 0
            WHERE id = ?
            """,
            (now, next_run_at, run_id, task["id"]),
        )
    else:
        await execute(
            """
            UPDATE scheduled_task
            SET last_run_at = ?, enabled = 0, locked_until = NULL,
                last_error = NULL, last_status = 'success', last_run_id = ?,
                consecutive_failures = 0
            WHERE id = ?
            """,
            (now, run_id, task["id"]),
        )


async def handle_task_failure(
    task: dict[str, Any],
    run_id: str | None,
    error: str,
    *,
    permanent: bool = False,
    app: TenantApp | None = None,
) -> None:
    error = (error or "未知错误")[:500]
    failures = int(task.get("consecutive_failures") or 0) + 1
    max_retries = int(
        task.get("max_retries") or get_settings().scheduler_max_retries
    )
    exhausted = permanent or failures > max_retries

    if run_id:
        await execute(
            """
            UPDATE scheduled_task_run
            SET status = 'error', finished_at = ?, error = ? WHERE run_id = ?
            """,
            (now_text(), error, run_id),
        )

    if exhausted:
        await execute(
            """
            UPDATE scheduled_task
            SET enabled = 0, locked_until = NULL, last_error = ?,
                last_status = 'error', consecutive_failures = ?, last_run_id = ?
            WHERE id = ?
            """,
            (error, failures, run_id, task["id"]),
        )
        if app:
            await best_effort_failure_notice(
                app,
                task,
                f"定时任务 #{task['id']}“{task.get('task_name') or '未命名任务'}”"
                f"已停止：{error}",
            )
        return

    delay = RETRY_DELAYS_SECONDS[
        min(failures - 1, len(RETRY_DELAYS_SECONDS) - 1)
    ]
    retry_at = (now_datetime() + timedelta(seconds=delay)).strftime(
        DB_DATETIME_FORMAT
    )
    await execute(
        """
        UPDATE scheduled_task
        SET next_run_at = ?, locked_until = NULL, last_error = ?,
            last_status = 'retrying', consecutive_failures = ?, last_run_id = ?
        WHERE id = ?
        """,
        (retry_at, error, failures, run_id, task["id"]),
    )


async def best_effort_failure_notice(
    app: TenantApp, task: dict[str, Any], message: str
) -> None:
    try:
        await send_message(
            app, task["chat_id"], msg_type="text", content={"text": message}
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to deliver scheduled task failure: task_id=%s", task["id"]
        )


def now_datetime() -> datetime:
    return datetime.now(SHANGHAI_TIMEZONE).replace(tzinfo=None)


def now_text() -> str:
    return now_datetime().strftime(DB_DATETIME_FORMAT)


def next_daily_run(daily_time: str) -> str:
    hour, minute = [int(part) for part in daily_time.split(":")]
    now = now_datetime()
    run_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if run_at <= now:
        run_at += timedelta(days=1)
    return run_at.strftime(DB_DATETIME_FORMAT)


def next_weekly_run(weekly_day: int, daily_time: str) -> str:
    if not 0 <= weekly_day <= 6:
        raise ValueError("weekly_day must be 0 to 6")
    hour, minute = [int(part) for part in daily_time.split(":")]
    now = now_datetime()
    run_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    run_at += timedelta(days=(weekly_day - now.weekday()) % 7)
    if run_at <= now:
        run_at += timedelta(days=7)
    return run_at.strftime(DB_DATETIME_FORMAT)


def next_weekly_days_run(weekly_days: str, daily_time: str) -> str:
    days = {int(day) for day in weekly_days.split(",")}
    if not days or any(day < 0 or day > 6 for day in days):
        raise ValueError("weekly_days must contain values from 0 to 6")
    hour, minute = [int(part) for part in daily_time.split(":")]
    now = now_datetime()
    for offset in range(8):
        run_at = (now + timedelta(days=offset)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if run_at.weekday() in days and run_at > now:
            return run_at.strftime(DB_DATETIME_FORMAT)
    raise ValueError("weekly_days did not produce a next run")


def next_interval_run(interval_minutes: int) -> str:
    run_at = now_datetime() + timedelta(minutes=max(1, interval_minutes))
    return run_at.replace(microsecond=0).strftime(DB_DATETIME_FORMAT)


def display_datetime(value: str | None) -> str:
    if not value:
        return "未设置"
    return value[:-3] if len(value) == 19 and value.endswith(":00") else value
