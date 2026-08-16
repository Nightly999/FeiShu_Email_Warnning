from __future__ import annotations

import uuid
from typing import Any

from app.db import open_db


def _scope(event: dict[str, Any]) -> tuple[str, str, str | None, str]:
    return (
        event["tenant_key"],
        event["app_id"],
        event.get("chat_id"),
        event["open_id"],
    )


async def get_active_session_id(
    *,
    tenant_key: str,
    app_id: str,
    open_id: str,
    chat_id: str | None,
    bot_code: str | None = None,
) -> str:
    async with open_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            """
            SELECT session_id
            FROM conversation_session
            WHERE tenant_key = ?
              AND app_id = ?
              AND open_id = ?
              AND (chat_id = ? OR (chat_id IS NULL AND ? IS NULL))
              AND active = 1
            ORDER BY id DESC
            LIMIT 1
            """,
            (tenant_key, app_id, open_id, chat_id, chat_id),
        )
        row = await cur.fetchone()
        if row:
            return str(row["session_id"])

        session_id = new_session_id()
        await db.execute(
            """
            INSERT INTO conversation_session (
              session_id, tenant_key, app_id, bot_code, chat_id, open_id, active
            ) VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (session_id, tenant_key, app_id, bot_code, chat_id, open_id),
        )
        return session_id


async def create_new_session(event: dict[str, Any]) -> str:
    tenant_key, app_id, chat_id, open_id = _scope(event)
    session_id = new_session_id()
    async with open_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            """
            UPDATE conversation_session
            SET active = 0, ended_at = CURRENT_TIMESTAMP
            WHERE tenant_key = ?
              AND app_id = ?
              AND open_id = ?
              AND (chat_id = ? OR (chat_id IS NULL AND ? IS NULL))
              AND active = 1
            """,
            (tenant_key, app_id, open_id, chat_id, chat_id),
        )
        await db.execute(
            """
            INSERT INTO conversation_session (
              session_id, tenant_key, app_id, bot_code, chat_id, open_id, active
            ) VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (session_id, tenant_key, app_id, event.get("bot_code"), chat_id, open_id),
        )
    return session_id


def new_session_id() -> str:
    return uuid.uuid4().hex


def short_session_id(session_id: str) -> str:
    return session_id[:8]
