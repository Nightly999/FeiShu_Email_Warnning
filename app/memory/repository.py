from __future__ import annotations

import json
from typing import Any

from app.db import open_db


def _json_dumps(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    return json.dumps(payload, ensure_ascii=False)


async def add_conversation_turn(
    *,
    tenant_key: str,
    app_id: str,
    open_id: str,
    role: str,
    content: str,
    session_id: str | None = None,
    bot_code: str | None = None,
    chat_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    async with open_db() as db:
        await db.execute(
            """
            INSERT INTO conversation_turn (
              tenant_key, app_id, bot_code, chat_id, open_id, session_id, role, content, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant_key,
                app_id,
                bot_code,
                chat_id,
                open_id,
                session_id,
                role,
                content,
                _json_dumps(metadata),
            ),
        )


async def fetch_recent_turns(
    *,
    tenant_key: str,
    app_id: str,
    open_id: str,
    chat_id: str | None,
    session_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    async with open_db() as db:
        cur = await db.execute(
            """
            SELECT role, content, created_at
            FROM conversation_turn
            WHERE tenant_key = ?
              AND app_id = ?
              AND open_id = ?
              AND (chat_id = ? OR (chat_id IS NULL AND ? IS NULL))
              AND session_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (tenant_key, app_id, open_id, chat_id, chat_id, session_id, limit),
        )
        rows = [dict(row) for row in await cur.fetchall()]
    return list(reversed(rows))


async def fetch_conversation_turn_by_message_id(
    *,
    tenant_key: str,
    app_id: str,
    open_id: str,
    chat_id: str | None,
    session_id: str,
    message_id: str,
) -> dict[str, Any] | None:
    async with open_db() as db:
        cur = await db.execute(
            """
            SELECT role, content, metadata, created_at
            FROM conversation_turn
            WHERE tenant_key = ?
              AND app_id = ?
              AND open_id = ?
              AND (chat_id = ? OR (chat_id IS NULL AND ? IS NULL))
              AND session_id = ?
              AND json_valid(metadata) = 1
              AND json_extract(metadata, '$.message_id') = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                tenant_key,
                app_id,
                open_id,
                chat_id,
                chat_id,
                session_id,
                message_id,
            ),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def add_long_memory(
    *,
    tenant_key: str,
    app_id: str,
    open_id: str,
    content: str,
    bot_code: str | None = None,
    chat_id: str | None = None,
    memory_type: str = "explicit",
    metadata: dict[str, Any] | None = None,
) -> None:
    async with open_db() as db:
        await db.execute(
            """
            INSERT INTO agent_memory (
              tenant_key, app_id, bot_code, chat_id, open_id,
              scope, memory_type, content, metadata
            ) VALUES (?, ?, ?, ?, ?, 'user_chat', ?, ?, ?)
            """,
            (
                tenant_key,
                app_id,
                bot_code,
                chat_id,
                open_id,
                memory_type,
                content,
                _json_dumps(metadata),
            ),
        )


async def fetch_long_memories(
    *,
    tenant_key: str,
    app_id: str,
    open_id: str,
    chat_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    async with open_db() as db:
        cur = await db.execute(
            """
            SELECT id, memory_type, content, created_at, updated_at
            FROM agent_memory
            WHERE tenant_key = ?
              AND app_id = ?
              AND open_id = ?
              AND enabled = 1
              AND (chat_id = ? OR (chat_id IS NULL AND ? IS NULL))
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (tenant_key, app_id, open_id, chat_id, chat_id, limit),
        )
        return [dict(row) for row in await cur.fetchall()]


async def disable_matching_memories(
    *,
    tenant_key: str,
    app_id: str,
    open_id: str,
    chat_id: str | None,
    keyword: str,
) -> int:
    pattern = f"%{keyword}%"
    async with open_db() as db:
        cur = await db.execute(
            """
            UPDATE agent_memory
            SET enabled = 0, updated_at = CURRENT_TIMESTAMP
            WHERE tenant_key = ?
              AND app_id = ?
              AND open_id = ?
              AND enabled = 1
              AND (chat_id = ? OR (chat_id IS NULL AND ? IS NULL))
              AND content LIKE ?
            """,
            (tenant_key, app_id, open_id, chat_id, chat_id, pattern),
        )
        return int(cur.rowcount or 0)
