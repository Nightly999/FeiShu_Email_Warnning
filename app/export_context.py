from __future__ import annotations

from typing import Any

from app.db import fetch_one, open_db


async def save_export_context(
    *,
    tenant_key: str,
    app_id: str,
    open_id: str,
    chat_id: str | None,
    session_id: str,
    source_type: str,
    source_ref: str,
    source_name: str | None = None,
) -> None:
    async with open_db() as db:
        await db.execute(
            """
            INSERT INTO export_context (
              tenant_key, app_id, open_id, chat_id, session_id,
              source_type, source_ref, source_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant_key,
                app_id,
                open_id,
                chat_id,
                session_id,
                source_type,
                source_ref,
                source_name,
            ),
        )


async def get_latest_export_context(
    *,
    tenant_key: str,
    app_id: str,
    open_id: str,
    chat_id: str | None,
    session_id: str,
) -> dict[str, Any] | None:
    return await fetch_one(
        """
        SELECT *
        FROM export_context
        WHERE tenant_key = ?
          AND app_id = ?
          AND open_id = ?
          AND (chat_id = ? OR (chat_id IS NULL AND ? IS NULL))
          AND session_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (tenant_key, app_id, open_id, chat_id, chat_id, session_id),
    )
