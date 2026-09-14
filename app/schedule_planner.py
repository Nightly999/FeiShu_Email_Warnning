from __future__ import annotations

import json
import logging
import re
from html import unescape
from datetime import datetime
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, ValidationError

from app.models.router import invoke_chat_with_fallback
from app.scheduler_runtime import DEFAULT_TIMEZONE, SHANGHAI_TIMEZONE


logger = logging.getLogger("schedule_planner")
SCHEDULE_TOOL_NAME = "plan_scheduled_task"
TEXT_TOOL_CALL_PATTERN = re.compile(
    rf"<tool_call>\s*<function\s*=\s*{SCHEDULE_TOOL_NAME}\s*>(.*?)"
    r"</function>\s*</tool_call>",
    re.DOTALL,
)
TEXT_TOOL_PARAMETER_PATTERN = re.compile(
    r"<parameter\s*=\s*([a-z_]+)\s*>(.*?)</parameter>",
    re.DOTALL,
)
SCHEDULE_PLAN_FIELDS = {
    "action",
    "schedule_type",
    "run_at",
    "daily_time",
    "interval_minutes",
    "prompt",
    "execution_mode",
    "task_name",
    "clarification",
}


class SchedulePlanError(RuntimeError):
    pass


class SchedulePlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal["create", "clarify"]
    schedule_type: Literal["once", "daily", "interval"] | None = None
    run_at: str | None = None
    daily_time: str | None = None
    interval_minutes: int | None = None
    prompt: str = ""
    execution_mode: Literal["agent", "reminder"] | None = None
    task_name: str | None = None
    clarification: str | None = None


SCHEDULE_TOOL = {
    "type": "function",
    "function": {
        "name": SCHEDULE_TOOL_NAME,
        "description": "把自然语言定时任务请求转换成可校验的结构化计划。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "clarify"]},
                "schedule_type": {
                    "type": ["string", "null"],
                    "enum": ["once", "daily", "interval", None],
                },
                "run_at": {"type": ["string", "null"]},
                "daily_time": {"type": ["string", "null"]},
                "interval_minutes": {"type": ["integer", "null"]},
                "prompt": {"type": "string"},
                "execution_mode": {
                    "type": ["string", "null"],
                    "enum": ["agent", "reminder", None],
                },
                "task_name": {"type": ["string", "null"]},
                "clarification": {"type": ["string", "null"]},
            },
            "required": [
                "action",
                "schedule_type",
                "run_at",
                "daily_time",
                "interval_minutes",
                "prompt",
                "execution_mode",
                "task_name",
                "clarification",
            ],
            "additionalProperties": False,
        },
    },
}


SCHEDULE_PLANNER_PROMPT = """
你是企业飞书助手的定时任务规划代理。你必须调用 plan_scheduled_task 工具，不能直接回答。

规则：
1. 从用户原始请求中提取执行时间和任务目标，不要生成代码、SQL 或 MCP 工具名。
2. daily 使用 24 小时制 HH:MM；once 使用 YYYY-MM-DD HH:MM；interval 使用整数分钟。
3. 需要届时查询、分析或汇总业务系统真实数据的任务使用 agent。只发送固定提醒文字的任务使用 reminder。
4. “查询预计日期前三天仍未完成并通知我”属于 agent，而“提醒我提交日报”属于 reminder。
5. prompt 保存届时交给 Agent 或提醒器的完整目标，删除“创建定时任务”“开始执行内容是”等外层措辞。
6. 如果用户明确补充了 Agent/提醒模式，以补充内容为准。
7. 缺少执行时间或任务内容时 action=clarify，并在 clarification 中只询问缺少的信息；不要猜测。
8. original_request 是引用回复链中的原始创建请求，follow_up 是用户当前补充。两者存在时合并理解。
"""


async def plan_schedule_creation(
    user_text: str,
    *,
    original_request: str | None = None,
) -> SchedulePlan:
    now = datetime.now(SHANGHAI_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "timezone": DEFAULT_TIMEZONE,
        "current_time": now,
        "original_request": original_request or "",
        "follow_up": user_text,
    }
    try:
        response = await invoke_chat_with_fallback(
            messages=[
                SystemMessage(content=SCHEDULE_PLANNER_PROMPT),
                HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
            ],
            route="text",
            tools=[SCHEDULE_TOOL],
            temperature=0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Schedule planning model failed")
        raise SchedulePlanError("定时任务规划模型暂时不可用，请稍后重试") from exc

    arguments = extract_schedule_plan_arguments(response)
    try:
        return SchedulePlan.model_validate(arguments)
    except ValidationError as exc:
        logger.warning("Invalid schedule plan: %s", exc)
        raise SchedulePlanError("模型返回的定时任务计划格式不正确") from exc


def extract_schedule_plan_arguments(response: Any) -> dict[str, Any]:
    for call in getattr(response, "tool_calls", None) or []:
        if call.get("name") != SCHEDULE_TOOL_NAME:
            continue
        arguments = call.get("args") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise SchedulePlanError("模型返回了无法解析的定时任务计划") from exc
        if isinstance(arguments, dict):
            return arguments
    content = getattr(response, "content", "")
    if isinstance(content, str):
        match = TEXT_TOOL_CALL_PATTERN.search(content)
        if match:
            arguments = {}
            for name, raw_value in TEXT_TOOL_PARAMETER_PATTERN.findall(match.group(1)):
                if name not in SCHEDULE_PLAN_FIELDS:
                    continue
                value = unescape(raw_value.strip())
                arguments[name] = (
                    None if value.casefold() in {"", "null", "none", "nil"} else value
                )
            if arguments:
                return arguments
    raise SchedulePlanError("模型没有调用定时任务规划工具，请重试")
