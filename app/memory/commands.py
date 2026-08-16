from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.memory.service import forget_from_event, list_memories_for_event, remember_from_event
from app.memory.sessions import create_new_session, short_session_id


MemoryAction = Literal["remember", "forget", "list", "new"]


@dataclass(frozen=True)
class MemoryCommand:
    action: MemoryAction
    content: str = ""


def parse_memory_command(text: str) -> MemoryCommand | None:
    normalized = (text or "").strip()
    if not normalized:
        return None

    if normalized.lower() in {"/new", "/reset", "/clear"} or normalized in {
        "新建会话",
        "创建新会话",
        "开启新会话",
        "重新开始",
        "清空上下文",
    }:
        return MemoryCommand(action="new")

    if normalized in {
        "查看记忆",
        "查看我的记忆",
        "查看长期记忆",
        "我的记忆",
        "记忆列表",
        "列出记忆",
        "看看记忆",
    }:
        return MemoryCommand(action="list")

    remember_match = re.match(
        r"^(?:请)?记住(?:一下)?[：:\s]*(.+)$", normalized, re.S
    )
    if remember_match:
        return MemoryCommand(action="remember", content=remember_match.group(1).strip())

    forget_match = re.match(
        r"^(?:忘记|忘掉|删除记忆)[：:\s]*(.+)$", normalized, re.S
    )
    if forget_match:
        return MemoryCommand(action="forget", content=forget_match.group(1).strip())

    return None


async def handle_memory_command(event: dict, text: str) -> str | None:
    command = parse_memory_command(text)
    if not command:
        return None
    if command.action == "remember":
        return await remember_from_event(event, command.content)
    if command.action == "forget":
        return await forget_from_event(event, command.content)
    if command.action == "new":
        session_id = await create_new_session(event)
        return f"已创建新会话 #{short_session_id(session_id)}。后续问题不会带入上一轮短期上下文。"
    return await list_memories_for_event(event)
