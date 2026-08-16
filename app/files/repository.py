from __future__ import annotations

from pathlib import Path
from typing import Any

from app.db import open_db


async def save_uploaded_file(
    *,
    tenant_key: str,
    app_id: str,
    open_id: str,
    path: Path,
    bot_code: str | None,
    chat_id: str | None,
    session_id: str | None,
    message_id: str | None,
    file_key: str | None,
    file_name: str | None,
    resource_type: str | None,
) -> None:
    async with open_db() as db:
        await db.execute(
            """
            INSERT INTO uploaded_file (
              tenant_key, app_id, bot_code, chat_id, open_id, session_id,
              message_id, file_key, file_name, resource_type, local_path, file_size
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant_key,
                app_id,
                bot_code,
                chat_id,
                open_id,
                session_id,
                message_id,
                file_key,
                file_name or path.name,
                resource_type,
                str(path),
                path.stat().st_size if path.exists() else 0,
            ),
        )


async def get_latest_uploaded_file(
    *,
    tenant_key: str,
    app_id: str,
    open_id: str,
    chat_id: str | None,
    session_id: str | None,
) -> dict[str, Any] | None:
    async with open_db() as db:
        cur = await db.execute(
            """
            SELECT *
            FROM uploaded_file
            WHERE tenant_key = ?
              AND app_id = ?
              AND open_id = ?
              AND (chat_id = ? OR (chat_id IS NULL AND ? IS NULL))
              AND (session_id = ? OR (session_id IS NULL AND ? IS NULL))
            ORDER BY id DESC
            LIMIT 1
            """,
            (tenant_key, app_id, open_id, chat_id, chat_id, session_id, session_id),
        )
        row = await cur.fetchone()
        return dict(row) if row else None
