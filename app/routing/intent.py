from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage

from app.models.router import invoke_chat_with_fallback


logger = logging.getLogger("feishu_intent")
IntentRoute = Literal["uploaded_file", "mcp", "chat", "clarify"]


@dataclass(frozen=True)
class IntentDecision:
    route: IntentRoute
    confidence: float
    reason: str


async def route_with_uploaded_file_context(user_text: str, uploaded_file: dict[str, Any]) -> IntentDecision:
    file_context = {
        "file_name": uploaded_file.get("file_name"),
        "resource_type": uploaded_file.get("resource_type"),
        "file_size": uploaded_file.get("file_size"),
        "created_at": uploaded_file.get("created_at"),
    }
    messages = [
        SystemMessage(content=INTENT_SYSTEM_PROMPT),
        HumanMessage(
            content=json.dumps(
                {
                    "user_text": user_text,
                    "latest_uploaded_file": file_context,
                },
                ensure_ascii=False,
            )
        ),
    ]
    try:
        response = await invoke_chat_with_fallback(messages=messages, route="text", temperature=0)
        return parse_intent_response(str(response.content or ""))
    except Exception:
        logger.exception("Intent routing failed, defaulting to MCP/chat path")
        return IntentDecision(route="mcp", confidence=0.0, reason="intent router failed")


def parse_intent_response(content: str) -> IntentDecision:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Invalid intent response: %s", content[:500])
        return IntentDecision(route="clarify", confidence=0.0, reason="invalid router response")

    route = str(payload.get("route") or "clarify")
    if route not in {"uploaded_file", "mcp", "chat", "clarify"}:
        route = "clarify"
    try:
        confidence = float(payload.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    reason = str(payload.get("reason") or "")
    return IntentDecision(route=route, confidence=confidence, reason=reason)


INTENT_SYSTEM_PROMPT = """
你是飞书企业智能体的意图路由器。你只判断下一步走哪条路径，不回答用户问题。

可选 route：
- uploaded_file：用户明显是在基于最近上传的文件/表格/图片/视频提问。
- mcp：用户是在查询企业系统业务数据，例如生产、样品、订单、库存、采购、OA、FR、WIP、欠料等。
- chat：普通闲聊、功能询问、解释说明，不需要文件也不需要业务工具。
- clarify：用户问题可能既可以基于文件，也可以查询系统数据，无法确定。

判断原则：
1. 最近上传文件只是上下文，不代表所有后续问题都要用文件。
2. 用户明确说“这份文件/这个表/刚才上传/附件/Excel/按表里数据”等，通常 route=uploaded_file。
3. 用户问企业系统中的业务对象或业务模块，通常 route=mcp，即使问题里有“分析/汇总/趋势”。
4. 如果用户说“销售趋势/销售数据”且最近上传文件名或上下文明显是销售数据，可以 route=uploaded_file。
5. 不确定时 route=clarify，不能猜。

只输出 JSON，不要 Markdown，不要解释正文：
{"route":"uploaded_file|mcp|chat|clarify","confidence":0.0到1.0,"reason":"一句简短原因"}
"""

