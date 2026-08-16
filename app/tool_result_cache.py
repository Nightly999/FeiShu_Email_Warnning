import json
from typing import Any

from app.db import fetch_one, open_db


async def save_tool_result(
    *,
    request_id: str,
    tenant_key: str,
    app_id: str,
    open_id: str,
    bot_code: str | None,
    message_id: str | None,
    chat_id: str | None,
    tool_name: str,
    tool_args: dict[str, Any],
    tool_result: str,
) -> int:
    async with open_db() as db:
        cur = await db.execute(
            """
            INSERT INTO agent_tool_result_cache (
              request_id, tenant_key, app_id, open_id, bot_code, message_id, chat_id,
              tool_name, tool_args, tool_result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                tenant_key,
                app_id,
                open_id,
                bot_code,
                message_id,
                chat_id,
                tool_name,
                json.dumps(tool_args, ensure_ascii=False),
                tool_result,
            ),
        )
        return int(cur.lastrowid)


async def get_tool_result_by_id(
    *,
    result_id: int,
    tenant_key: str,
    app_id: str,
    open_id: str,
    chat_id: str | None,
) -> dict[str, Any] | None:
    return await fetch_one(
        """
        SELECT *
        FROM agent_tool_result_cache
        WHERE id = ? AND tenant_key = ? AND app_id = ? AND open_id = ?
          AND (chat_id = ? OR (chat_id IS NULL AND ? IS NULL))
        """,
        (result_id, tenant_key, app_id, open_id, chat_id, chat_id),
    )
