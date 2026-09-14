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
    request_message_id: str | None = None,
    reply_message_id: str | None = None,
) -> None:
    async with open_db() as db:
        await db.execute(
            """
            INSERT INTO export_context (
              tenant_key, app_id, open_id, chat_id, session_id,
              source_type, source_ref, source_name,
              request_message_id, reply_message_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                request_message_id,
                reply_message_id,
            ),
        )


async def get_latest_export_context(
    *,
    tenant_key: str,
    app_id: str,
    open_id: str,
    chat_id: str | None,
    session_id: str,
    referenced_message_ids: list[str] | None = None,
) -> dict[str, Any] | None:
    message_ids = list(dict.fromkeys(item for item in referenced_message_ids or [] if item))
    if message_ids:
        placeholders = ", ".join("?" for _ in message_ids)
        return await fetch_one(
            f"""
            SELECT context.*
            FROM export_context AS context
            LEFT JOIN agent_tool_result_cache AS cache
              ON context.source_type = 'tool_result'
             AND CAST(cache.id AS TEXT) = context.source_ref
            WHERE context.tenant_key = ?
              AND context.app_id = ?
              AND context.open_id = ?
              AND (context.chat_id = ? OR (context.chat_id IS NULL AND ? IS NULL))
              AND context.session_id = ?
              AND (
                context.request_message_id IN ({placeholders})
                OR context.reply_message_id IN ({placeholders})
                OR cache.message_id IN ({placeholders})
              )
            ORDER BY context.id DESC
            LIMIT 1
            """,
            (
                tenant_key,
                app_id,
                open_id,
                chat_id,
                chat_id,
                session_id,
                *message_ids,
                *message_ids,
                *message_ids,
            ),
        )
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
