import json
from typing import Any

from app.db import execute


async def write_audit(
    *,
    request_id: str,
    tenant_key: str,
    app_id: str,
    open_id: str,
    internal_username: str | None,
    bot_code: str | None = None,
    message_id: str | None = None,
    chat_id: str | None = None,
    user_message: str | None = None,
    tool_name: str | None = None,
    permission_result: str | None = None,
    tool_args: dict[str, Any] | None = None,
    tool_result_summary: str | None = None,
) -> None:
    await execute(
        """
        INSERT INTO agent_audit_log (
          request_id, tenant_key, app_id, open_id, internal_username,
          bot_code, message_id, chat_id,
          user_message, tool_name, permission_result, tool_args, tool_result_summary
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request_id,
            tenant_key,
            app_id,
            open_id,
            internal_username,
            bot_code,
            message_id,
            chat_id,
            user_message,
            tool_name,
            permission_result,
            json.dumps(tool_args, ensure_ascii=False) if tool_args else None,
            tool_result_summary,
        ),
    )
