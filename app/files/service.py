from __future__ import annotations

import json
from pathlib import Path

from app.export_context import save_export_context
from app.files.analysis import analyze_file_for_question, build_upload_ack
from app.files.repository import get_latest_uploaded_file, save_uploaded_file
from app.memory.sessions import get_active_session_id
from app.routing.intent import route_with_uploaded_file_context


async def register_uploaded_file(event: dict, path: Path) -> str:
    session_id = await event_session_id(event)
    await save_uploaded_file(
        tenant_key=event["tenant_key"],
        app_id=event["app_id"],
        bot_code=event.get("bot_code"),
        chat_id=event.get("chat_id"),
        session_id=session_id,
        open_id=event["open_id"],
        message_id=event.get("message_id"),
        file_key=event.get("file_key"),
        file_name=event.get("file_name"),
        resource_type=event.get("resource_type"),
        path=path,
    )
    await save_export_context(
        tenant_key=event["tenant_key"],
        app_id=event["app_id"],
        open_id=event["open_id"],
        chat_id=event.get("chat_id"),
        session_id=session_id,
        source_type="uploaded_file",
        source_ref=str(path),
        source_name=event.get("file_name") or path.name,
    )
    return build_upload_ack(path)


async def answer_from_recent_file(event: dict, text: str) -> str | None:
    session_id = await event_session_id(event)
    latest = await get_latest_uploaded_file(
        tenant_key=event["tenant_key"],
        app_id=event["app_id"],
        open_id=event["open_id"],
        chat_id=event.get("chat_id"),
        session_id=session_id,
    )
    if not latest:
        return None
    decision = await route_with_uploaded_file_context(text, latest)
    if decision.route == "clarify":
        return (
            "你是想基于刚才上传的文件分析，还是查询系统里的业务数据？\n\n"
            "可以回复：\n"
            "- 基于文件分析\n"
            "- 查询系统数据"
        )
    if decision.route != "uploaded_file":
        return None
    path = Path(latest["local_path"])
    if not path.exists():
        return f"找到了最近上传记录，但本地文件不存在：{latest.get('file_name') or path.name}"
    answer = await analyze_file_for_question(path, text)
    await save_export_context(
        tenant_key=event["tenant_key"],
        app_id=event["app_id"],
        open_id=event["open_id"],
        chat_id=event.get("chat_id"),
        session_id=session_id,
        source_type="analysis_result",
        source_ref=json.dumps(
            {"answer": answer, "source_path": str(path)}, ensure_ascii=False
        ),
        source_name=latest.get("file_name") or path.name,
    )
    return answer


async def event_session_id(event: dict) -> str:
    session_id = event.get("_session_id")
    if session_id:
        return str(session_id)
    return await get_active_session_id(
        tenant_key=event["tenant_key"],
        app_id=event["app_id"],
        open_id=event["open_id"],
        chat_id=event.get("chat_id"),
        bot_code=event.get("bot_code"),
    )
