from __future__ import annotations

import json
import logging
import math
import re
import uuid
from typing import Any, Literal

from app.db import fetch_one, open_db
from app.identity import resolve_identity
from app.mcp_client import McpClient
from app.policy import check_agent_access, check_tool_access, inject_identity_args
from app.settings import get_settings
from app.tool_result_cache import get_tool_result_by_id, save_tool_result


PageDirection = Literal["next", "previous"]
logger = logging.getLogger("business_pagination")


def parse_business_page_command(text: str) -> dict[str, Any] | None:
    normalized = re.sub(r"[。！？!?]+$", "", (text or "").strip()).strip()
    if normalized in {
        "下一页",
        "下页",
        "查看下一页",
        "下一批",
        "下一个页",
        "翻到下一页",
        "往后翻一页",
    }:
        return {"direction": "next"}
    if normalized in {
        "上一页",
        "上页",
        "查看上一页",
        "上一批",
        "上一个页",
        "翻到上一页",
        "往前翻一页",
    }:
        return {"direction": "previous"}
    match = re.match(
        r"^(?:(?:业务明细|查询结果|列表)\s*)?(?:查看|跳到|翻到)?"
        r"第?\s*(\d+)\s*(?:页|批)$",
        normalized,
    )
    if match:
        return {"page": int(match.group(1))}
    return None


async def register_business_result(
    *,
    tenant_key: str,
    app_id: str,
    open_id: str,
    chat_id: str | None,
    session_id: str,
    result_id: int,
    tool_name: str | None,
    tool_result: str,
) -> int:
    rows = extract_rows(tool_result)
    pagination = extract_pagination(tool_result)
    current_page = pagination["page"] if pagination else 1
    total_rows = pagination["totalCount"] if pagination else len(rows)
    async with open_db() as db:
        await db.execute(
            """
            INSERT INTO business_list_cursor (
              tenant_key, app_id, open_id, chat_scope, session_id,
              result_id, tool_name, current_page, total_rows
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (tenant_key, app_id, open_id, chat_scope, session_id)
            DO UPDATE SET
              result_id = excluded.result_id,
              tool_name = excluded.tool_name,
              current_page = 1,
              total_rows = excluded.total_rows,
              updated_at = CURRENT_TIMESTAMP
            """,
            (
                tenant_key,
                app_id,
                open_id,
                chat_scope(chat_id),
                session_id,
                result_id,
                tool_name,
                current_page,
                total_rows,
            ),
        )
    return total_rows


async def render_business_page(
    *,
    tenant_key: str,
    app_id: str,
    open_id: str,
    chat_id: str | None,
    session_id: str,
    page: int | None = None,
    direction: PageDirection | None = None,
    expected_result_id: int | None = None,
) -> str | None:
    cursor = await fetch_one(
        """
        SELECT * FROM business_list_cursor
        WHERE tenant_key = ? AND app_id = ? AND open_id = ?
          AND chat_scope = ? AND session_id = ?
        """,
        (tenant_key, app_id, open_id, chat_scope(chat_id), session_id),
    )
    if not cursor:
        return None
    if expected_result_id is not None and cursor["result_id"] != expected_result_id:
        return None

    cached = await get_tool_result_by_id(
        result_id=int(cursor["result_id"]),
        tenant_key=tenant_key,
        app_id=app_id,
        open_id=open_id,
        chat_id=chat_id,
    )
    if not cached:
        return None
    rows = extract_rows(cached.get("tool_result") or "")
    if not rows:
        return None

    pagination = extract_pagination(cached.get("tool_result") or "")
    settings = get_settings()
    if pagination:
        page_size = pagination["pageSize"]
        total_rows = pagination["totalCount"]
        total_pages = max(1, pagination["totalPages"])
    else:
        page_size = min(max(settings.business_list_page_size, 1), 20)
        total_rows = len(rows)
        total_pages = max(1, math.ceil(total_rows / page_size))
    current_page = int(cursor.get("current_page") or 1)
    requested_page = page or current_page
    if direction == "next":
        requested_page = current_page + 1
    elif direction == "previous":
        requested_page = current_page - 1

    if requested_page < 1 or requested_page > total_pages:
        boundary = "第一批" if requested_page < 1 else "最后一批"
        return (
            f"已经是{boundary}。当前业务明细共 {total_rows} 条、"
            f"{total_pages} 批，当前在第 {current_page} 批。"
        )

    if pagination:
        if requested_page != pagination["page"]:
            fetched = await fetch_remote_page(
                cursor=cursor,
                cached=cached,
                requested_page=requested_page,
                page_size=page_size,
            )
            if isinstance(fetched, str):
                return fetched
            rows, pagination = fetched
            total_rows = pagination["totalCount"]
            total_pages = max(1, pagination["totalPages"])
        page_rows = rows
        start = (requested_page - 1) * page_size
    else:
        await update_current_page(int(cursor["id"]), requested_page)
        start = (requested_page - 1) * page_size
        page_rows = rows[start : start + page_size]
    return format_business_page(
        page_rows,
        all_headers=collect_headers(page_rows),
        start_index=start + 1,
        page=requested_page,
        total_pages=total_pages,
        total_rows=total_rows,
    )


async def fetch_remote_page(
    *,
    cursor: dict[str, Any],
    cached: dict[str, Any],
    requested_page: int,
    page_size: int,
) -> tuple[list[dict[str, Any]], dict[str, int]] | str:
    identity = await resolve_identity(
        tenant_key=str(cached["tenant_key"]),
        app_id=str(cached["app_id"]),
        open_id=str(cached["open_id"]),
        union_id=None,
        user_id=None,
    )
    agent_policy = check_agent_access(identity)
    if not agent_policy.allowed:
        return agent_policy.reason

    try:
        tool_args = json.loads(cached.get("tool_args") or "{}")
    except (TypeError, json.JSONDecodeError):
        tool_args = {}
    if not isinstance(tool_args, dict):
        tool_args = {}
    tool_args["page"] = requested_page
    tool_args["pageSize"] = page_size
    protected_args = inject_identity_args(identity, tool_args)
    tool_policy = check_tool_access(identity, str(cached["tool_name"]), protected_args)
    if not tool_policy.allowed:
        return tool_policy.reason

    try:
        tool_result = await McpClient().call_tool(
            str(cached["tool_name"]), protected_args
        )
        rows = extract_rows(tool_result)
        pagination = extract_pagination(tool_result)
        if not rows or not pagination or pagination["page"] != requested_page:
            raise ValueError("MCP returned invalid pagination data")
        result_id = await save_tool_result(
            request_id=f"page-{uuid.uuid4()}",
            tenant_key=identity.tenant_key,
            app_id=identity.app_id,
            open_id=identity.open_id,
            bot_code=cached.get("bot_code"),
            message_id=cached.get("message_id"),
            chat_id=cached.get("chat_id"),
            tool_name=str(cached["tool_name"]),
            tool_args=protected_args,
            tool_result=tool_result,
        )
        await update_remote_cursor(
            int(cursor["id"]),
            result_id=result_id,
            page=requested_page,
            total_rows=pagination["totalCount"],
        )
        return rows, pagination
    except Exception:  # noqa: BLE001
        logger.exception(
            "Remote MCP pagination failed: tool=%s page=%s",
            cached.get("tool_name"),
            requested_page,
        )
        return "业务明细翻页失败，请稍后重试。当前页未发生变化。"


async def update_current_page(cursor_id: int, page: int) -> None:
    async with open_db() as db:
        await db.execute(
            """
            UPDATE business_list_cursor
            SET current_page = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (page, cursor_id),
        )


async def update_remote_cursor(
    cursor_id: int, *, result_id: int, page: int, total_rows: int
) -> None:
    async with open_db() as db:
        await db.execute(
            """
            UPDATE business_list_cursor
            SET result_id = ?, current_page = ?, total_rows = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (result_id, page, total_rows, cursor_id),
        )


def format_business_page(
    rows: list[dict[str, Any]],
    *,
    all_headers: list[str],
    start_index: int,
    page: int,
    total_pages: int,
    total_rows: int,
) -> str:
    settings = get_settings()
    # Feishu allows at most 50 table columns. Reserve the first one for the
    # frozen sequence number so the configured value always means total columns.
    max_table_columns = min(max(settings.business_list_max_columns, 2), 50)
    max_data_columns = max_table_columns - 1
    headers = all_headers[:max_data_columns]
    table_headers = ["序号", *headers]
    lines = [
        f"业务明细（第 {page}/{total_pages} 批，共 {total_rows} 条）",
        "| " + " | ".join(escape_cell(header) for header in table_headers) + " |",
        "|" + "|".join("---" for _ in table_headers) + "|",
    ]
    for offset, row in enumerate(rows):
        values = [str(start_index + offset)]
        values.extend(format_cell(row.get(header)) for header in headers)
        lines.append("| " + " | ".join(escape_cell(value) for value in values) + " |")

    commands: list[str] = []
    if page > 1:
        commands.append("上一批：发送“上一页”")
    if page < total_pages:
        commands.append("下一批：发送“下一页”")
    commands.append(f"跳转批次：发送“第 N 页”（1-{total_pages}）")
    lines.extend(["", *commands])
    if len(all_headers) > max_data_columns:
        lines.append("部分次要字段未在卡片中展示，可发送“导出 Excel”查看完整字段。")
    return "\n".join(lines)


def extract_rows(tool_result: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(tool_result)
    except (TypeError, json.JSONDecodeError):
        return []
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def extract_pagination(tool_result: str) -> dict[str, int] | None:
    try:
        payload = json.loads(tool_result)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        pagination = {
            "page": int(payload["page"]),
            "pageSize": int(payload["pageSize"]),
            "totalCount": int(payload["totalCount"]),
            "totalPages": int(payload["totalPages"]),
        }
    except (KeyError, TypeError, ValueError):
        return None
    if (
        pagination["page"] < 1
        or pagination["pageSize"] < 1
        or pagination["totalCount"] < 0
        or pagination["totalPages"] < 0
    ):
        return None
    return pagination


def collect_headers(rows: list[dict[str, Any]]) -> list[str]:
    headers: list[str] = []
    for row in rows:
        for key in row:
            key_text = str(key)
            if key_text not in headers:
                headers.append(key_text)
    return headers


def format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    return text if len(text) <= 80 else text[:77] + "..."


def escape_cell(value: str) -> str:
    return str(value).replace("|", "｜").replace("\n", " ")


def chat_scope(chat_id: str | None) -> str:
    return chat_id or ""
