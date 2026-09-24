from __future__ import annotations

import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage

from app.models.router import invoke_chat_with_fallback


logger = logging.getLogger("multi_intent")
_MULTI_MARKER = re.compile(
    r"[;；\n]|然后|接着|随后|另外|此外|同时|"
    r"并(?:且|再|请|帮我|查询|查看|列出|创建|设置|取消|删除|分析)|"
    r"先.+?(?:再|然后)",
    re.S,
)
_FALLBACK_SPLIT = re.compile(
    r"\s*(?:[;；\n]+|，?然后|，?接着|，?随后|，?另外|，?此外|，?同时还(?:要)?|，?并且还(?:要)?)\s*"
)


async def split_multi_intent_commands(text: str) -> list[str]:
    text = (text or "").strip()
    if not text or not _MULTI_MARKER.search(text):
        return [text]

    fallback = _fallback_commands(text)
    tool = {
        "type": "function",
        "function": {
            "name": "split_user_commands",
            "description": "把一段话中的多个独立指令按执行顺序拆开。",
            "parameters": {
                "type": "object",
                "properties": {
                    "commands": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 5,
                    }
                },
                "required": ["commands"],
                "additionalProperties": False,
            },
        },
    }
    prompt = (
        "只拆分能够独立执行的指令，保持原顺序和原意，不新增内容。"
        "共享同一目标或结果的连续动作不要拆分，例如‘分析邮件并推送结果’、"
        "‘查询数据并汇总’、‘周一到周五17:30分析邮件并推送’都只有一条。"
        "‘先分析邮件，然后列出定时任务’是两条。若不是多指令，commands 只返回原文。"
    )
    try:
        response = await invoke_chat_with_fallback(
            messages=[SystemMessage(content=prompt), HumanMessage(content=text)],
            route="text",
            tools=[tool],
            temperature=0,
        )
        calls = getattr(response, "tool_calls", None) or []
        commands = (calls[0].get("args") or {}).get("commands") if calls else None
        if isinstance(commands, list):
            commands = [str(command).strip() for command in commands if str(command).strip()]
            if 1 <= len(commands) <= 5 and _ordered_substrings(text, commands):
                return commands
    except Exception:
        logger.warning("Multi-intent planning failed; using deterministic split", exc_info=True)
    return fallback


def _ordered_substrings(text: str, commands: list[str]) -> bool:
    position = 0
    for command in commands:
        position = text.find(command, position)
        if position < 0:
            return False
        position += len(command)
    return True


def _fallback_commands(text: str) -> list[str]:
    first_then = re.match(r"^\s*先\s*(.+?)\s*[，,]?\s*(?:再|然后)\s*(.+)$", text, re.S)
    if first_then:
        return [first_then.group(1).strip(), first_then.group(2).strip()]
    commands = [part.strip() for part in _FALLBACK_SPLIT.split(text) if part.strip()]
    return commands[:5] if len(commands) > 1 else [text]
