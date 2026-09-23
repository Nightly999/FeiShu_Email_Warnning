from __future__ import annotations

import logging
from typing import Any

from app.business_pagination import parse_business_page_command, render_business_page
from app.email_repository import finalize_push_logs
from app.email_service import handle_email_command
from app.excel_design import ExcelDesignError
from app.excel_export import export_latest_result_to_excel
from app.feishu import (
    TenantApp,
    reply_card,
    reply_file,
    update_card,
    upload_file,
)
from app.feishu_cards import build_answer_card
from app.files.service import answer_from_recent_file
from app.memory.commands import handle_memory_command
from app.memory.service import record_agent_exchange
from app.memory.sessions import get_active_session_id
from app.routing.export_intent import should_export_excel
from app.reply_context import referenced_message_ids
from app.scheduler import (
    handle_schedule_command,
    is_schedule_followup_reply,
    recent_schedule_creation_request,
    resolve_schedule_command,
)


logger = logging.getLogger("builtin_commands")


async def deliver_builtin_answer(
    app: TenantApp,
    *,
    message_id: str,
    progress_message_id: str | None,
    question: str,
    answer: str,
    status: str = "success",
    title: str | None = None,
) -> None:
    card = build_answer_card(question, answer, status=status, title=title)
    if progress_message_id:
        if await update_card(app, progress_message_id, card):
            return
    await reply_card(app, message_id, card)


async def handle_builtin_text_command(
    app: TenantApp,
    event: dict[str, Any],
    progress_message_id: str | None,
) -> bool:
    text = event.get("text") or ""
    message_id = event.get("message_id")
    if not message_id:
        return False

    memory_answer = await handle_memory_command(event, text)
    if memory_answer:
        await deliver_builtin_answer(
            app,
            message_id=message_id,
            progress_message_id=progress_message_id,
            question=text,
            answer=memory_answer,
        )
        return True

    page_command = parse_business_page_command(text)
    if page_command:
        session_id = str(event.get("_session_id") or "")
        page_answer = await render_business_page(
            tenant_key=event["tenant_key"],
            app_id=event["app_id"],
            open_id=event["open_id"],
            chat_id=event.get("chat_id"),
            session_id=session_id,
            page=page_command.get("page"),
            direction=page_command.get("direction"),
        )
        answer = page_answer or (
            "当前会话没有可翻页的业务明细。请先查询业务列表，再发送“下一页”。"
        )
        await record_agent_exchange(event, answer, session_id=session_id or None)
        await deliver_builtin_answer(
            app,
            message_id=message_id,
            progress_message_id=progress_message_id,
            question=text,
            answer=answer,
        )
        return True

    if await should_export_excel(text):
        session_id = str(event.get("_session_id") or "")
        if not session_id:
            session_id = await get_active_session_id(
                tenant_key=event["tenant_key"],
                app_id=event["app_id"],
                open_id=event["open_id"],
                chat_id=event.get("chat_id"),
                bot_code=event.get("bot_code"),
            )
        try:
            path = await export_latest_result_to_excel(
                tenant_key=event["tenant_key"],
                app_id=event["app_id"],
                open_id=event["open_id"],
                chat_id=event.get("chat_id"),
                session_id=session_id,
                request_text=text,
                referenced_message_ids=referenced_message_ids(event),
            )
        except ExcelDesignError as exc:
            answer = f"无法按当前要求生成 Excel：{exc}"
            await deliver_builtin_answer(
                app,
                message_id=message_id,
                progress_message_id=progress_message_id,
                question=text,
                answer=answer,
            )
            await record_agent_exchange(event, answer, session_id=session_id)
            return True
        if not path:
            if event.get("parent_id"):
                answer = "被引用的消息没有关联到可导出的查询结果。"
            else:
                answer = (
                    "没有找到可导出的最近一次查询结果。"
                    "请先查询数据，再发送“导出 Excel”。"
                )
            await deliver_builtin_answer(
                app,
                message_id=message_id,
                progress_message_id=progress_message_id,
                question=text,
                answer=answer,
            )
            return True
        file_key = await upload_file(app, path)
        await reply_file(app, message_id, file_key)
        export_answer = f"已根据你的要求生成 Excel：{path.name}"
        await deliver_builtin_answer(
            app,
            message_id=message_id,
            progress_message_id=progress_message_id,
            question=text,
            answer=export_answer,
        )
        await record_agent_exchange(event, export_answer)
        return True

    recent_schedule_request = None
    if not event.get("_referenced_request_text") and is_schedule_followup_reply(text):
        recent_schedule_request = await recent_schedule_creation_request(event)
    command = await resolve_schedule_command(
        text,
        referenced_message_text=event.get("_referenced_message_text"),
        referenced_request_text=(
            event.get("_referenced_request_text") or recent_schedule_request
        ),
    )
    if command:
        answer = await handle_schedule_command(app, event, command)
        await record_agent_exchange(
            event,
            answer,
            session_id=str(event.get("_session_id") or "") or None,
        )
        await deliver_builtin_answer(
            app,
            message_id=message_id,
            progress_message_id=progress_message_id,
            question=text,
            answer=answer,
            status="error" if command.get("schedule_type") == "invalid" else "success",
            title=(
                "邮箱定时设置失败"
                if command.get("schedule_type") == "invalid"
                and any(word in text.casefold() for word in ("邮件", "邮箱", "email", "mail"))
                else "定时任务"
            ),
        )
        return True

    email_result = await handle_email_command(event)
    if email_result:
        try:
            if email_result.card:
                if progress_message_id and await update_card(
                    app, progress_message_id, email_result.card
                ):
                    pass
                else:
                    await reply_card(app, message_id, email_result.card)
            else:
                await deliver_builtin_answer(
                    app,
                    message_id=message_id,
                    progress_message_id=progress_message_id,
                    question=text,
                    answer=email_result.answer,
                    status=email_result.status,
                )
            if email_result.run_ref:
                try:
                    await finalize_push_logs(email_result.run_ref, True)
                except Exception:
                    logger.exception("Failed to finalize manual email push log")
        except Exception as exc:
            if email_result.run_ref:
                try:
                    await finalize_push_logs(
                        email_result.run_ref, False, type(exc).__name__
                    )
                except Exception:
                    logger.exception("Failed to record manual email delivery failure")
            raise
        await record_agent_exchange(event, email_result.answer)
        return True

    file_answer = await answer_from_recent_file(event, text)
    if file_answer:
        await record_agent_exchange(event, file_answer)
        await deliver_builtin_answer(
            app,
            message_id=message_id,
            progress_message_id=progress_message_id,
            question=text,
            answer=file_answer,
        )
        return True

    return False
