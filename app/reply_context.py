from __future__ import annotations

import json
import logging
from typing import Any

from app.feishu import TenantApp, extract_card_text, get_message
from app.memory.repository import fetch_conversation_turn_by_message_id


logger = logging.getLogger("feishu_reply_context")
MAX_REFERENCED_TEXT_CHARS = 6000


async def hydrate_reply_context(app: TenantApp, event: dict[str, Any]) -> None:
    referenced_message_id = str(event.get("parent_id") or "").strip()
    if not referenced_message_id:
        return

    session_id = str(event.get("_session_id") or "")
    if session_id:
        local_turn = await fetch_conversation_turn_by_message_id(
            tenant_key=event["tenant_key"],
            app_id=event["app_id"],
            open_id=event["open_id"],
            chat_id=event.get("chat_id"),
            session_id=session_id,
            message_id=referenced_message_id,
        )
        if local_turn:
            event["_referenced_message_text"] = clip_referenced_text(
                str(local_turn.get("content") or "")
            )
            metadata = parse_metadata(local_turn.get("metadata"))
            request_message_id = metadata.get("request_message_id")
            if request_message_id:
                event["_referenced_parent_id"] = str(request_message_id)
                request_turn = await fetch_conversation_turn_by_message_id(
                    tenant_key=event["tenant_key"],
                    app_id=event["app_id"],
                    open_id=event["open_id"],
                    chat_id=event.get("chat_id"),
                    session_id=session_id,
                    message_id=str(request_message_id),
                )
                if request_turn and request_turn.get("role") == "user":
                    event["_referenced_request_text"] = clip_referenced_text(
                        str(request_turn.get("content") or "")
                    )
                elif not request_turn:
                    await hydrate_original_request_from_api(
                        app, event, str(request_message_id)
                    )
            return

    try:
        message = await get_message(app, referenced_message_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to load referenced Feishu message: message_id=%s",
            referenced_message_id,
        )
        return
    if not message:
        return
    if event.get("chat_id") and message.get("chat_id") != event.get("chat_id"):
        logger.warning(
            "Ignore referenced message from another chat: message_id=%s",
            referenced_message_id,
        )
        return

    text = extract_api_message_text(message)
    if text:
        event["_referenced_message_text"] = clip_referenced_text(text)
    if message.get("parent_id"):
        event["_referenced_parent_id"] = str(message["parent_id"])
    if message.get("root_id"):
        event["_referenced_root_id"] = str(message["root_id"])
    original_message_id = str(
        message.get("parent_id") or message.get("root_id") or ""
    ).strip()
    if original_message_id and original_message_id != referenced_message_id:
        await hydrate_original_request_from_api(app, event, original_message_id)


async def hydrate_original_request_from_api(
    app: TenantApp, event: dict[str, Any], message_id: str
) -> None:
    try:
        message = await get_message(app, message_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to load original Feishu request: message_id=%s", message_id
        )
        return
    if not message:
        return
    if event.get("chat_id") and message.get("chat_id") != event.get("chat_id"):
        logger.warning(
            "Ignore original request from another chat: message_id=%s", message_id
        )
        return
    text = extract_api_message_text(message)
    if text:
        event["_referenced_request_text"] = clip_referenced_text(text)


def referenced_message_ids(event: dict[str, Any]) -> list[str]:
    values = (
        event.get("parent_id"),
        event.get("root_id"),
        event.get("_referenced_parent_id"),
        event.get("_referenced_root_id"),
    )
    result: list[str] = []
    for value in values:
        message_id = str(value or "").strip()
        if message_id and message_id not in result:
            result.append(message_id)
    return result


def extract_api_message_text(message: dict[str, Any]) -> str:
    body = message.get("body") or {}
    content = body.get("content") if isinstance(body, dict) else None
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return content.strip()
    elif isinstance(content, dict):
        parsed = content
    else:
        return ""

    if not isinstance(parsed, dict):
        return str(parsed).strip()
    if message.get("msg_type") == "interactive" or (
        "elements" in parsed or "body" in parsed or "header" in parsed
    ):
        return extract_card_text(parsed)
    return str(parsed.get("text") or parsed.get("content") or "").strip()


def parse_metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def clip_referenced_text(text: str) -> str:
    normalized = text.strip()
    if len(normalized) <= MAX_REFERENCED_TEXT_CHARS:
        return normalized
    return normalized[: MAX_REFERENCED_TEXT_CHARS - 3] + "..."
