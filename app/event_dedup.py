from __future__ import annotations

import time

from app.db import execute, fetch_all, open_db


async def claim_event(
    *, tenant_key: str, app_id: str, message_id: str, stale_after_seconds: int = 600
) -> bool:
    """Claim a Feishu message once, while allowing failed or abandoned work to retry."""
    now = int(time.time())
    async with open_db() as db:
        cur = await db.execute(
            """
            INSERT OR IGNORE INTO processed_event (
              tenant_key, app_id, message_id, status, created_at, updated_at
            ) VALUES (?, ?, ?, 'processing', ?, ?)
            """,
            (tenant_key, app_id, message_id, now, now),
        )
        if int(cur.rowcount or 0) > 0:
            return True

        cur = await db.execute(
            """
            UPDATE processed_event
            SET status = 'processing', updated_at = ?, last_error = NULL
            WHERE tenant_key = ? AND app_id = ? AND message_id = ?
              AND (status = 'failed' OR (status = 'processing' AND updated_at < ?))
            """,
            (now, tenant_key, app_id, message_id, now - stale_after_seconds),
        )
        return int(cur.rowcount or 0) > 0


async def finish_event(
    *, tenant_key: str, app_id: str, message_id: str, error: str | None = None
) -> None:
    await execute(
        """
        UPDATE processed_event
        SET status = ?, updated_at = ?, last_error = ?
        WHERE tenant_key = ? AND app_id = ? AND message_id = ?
        """,
        (
            "failed" if error else "completed",
            int(time.time()),
            (error or "")[:500] or None,
            tenant_key,
            app_id,
            message_id,
        ),
    )


async def record_event_progress(
    *, tenant_key: str, app_id: str, message_id: str,
    progress_message_id: str, request_text: str,
) -> None:
    await execute(
        """
        UPDATE processed_event
        SET progress_message_id = ?, request_text = ?, updated_at = ?
        WHERE tenant_key = ? AND app_id = ? AND message_id = ?
          AND status = 'processing'
        """,
        (
            progress_message_id,
            request_text[:500],
            int(time.time()),
            tenant_key,
            app_id,
            message_id,
        ),
    )


async def list_processing_events(
    *, tenant_key: str, app_id: str
) -> list[dict]:
    return await fetch_all(
        """
        SELECT message_id, progress_message_id, request_text
        FROM processed_event
        WHERE tenant_key = ? AND app_id = ? AND status = 'processing'
        """,
        (tenant_key, app_id),
    )
