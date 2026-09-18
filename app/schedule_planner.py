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
    "task_id",
    "page",
    "schedule_type",
    "run_at",
    "daily_time",
    "daily_times",
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

    action: Literal[
        "create", "list", "history", "cancel_all", "cancel", "pause",
        "resume", "run_now", "clarify",
    ]
    task_id: int | None = None
    page: int | None = None
    schedule_type: Literal["once", "daily", "daily_multi", "interval"] | None = None
    run_at: str | None = None
    daily_time: str | None = None
    daily_times: list[str] | None = None
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
                "action": {
                    "type": "string",
                    "enum": [
                        "create", "list", "history", "cancel_all", "cancel",
                        "pause", "resume", "run_now", "clarify",
                    ],
                },
                "task_id": {"type": ["integer", "null"]},
                "page": {"type": ["integer", "null"]},
                "schedule_type": {
                    "type": ["string", "null"],
                    "enum": ["once", "daily", "daily_multi", "interval", None],
                },
                "run_at": {"type": ["string", "null"]},
                "daily_time": {"type": ["string", "null"]},
                "daily_times": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                },
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
                "task_id",
                "page",
                "schedule_type",
                "run_at",
                "daily_time",
                "daily_times",
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
1. 先识别用户要创建还是管理定时任务。查询、列出、有几个、有哪些任务使用 list；查看执行记录使用 history；删除全部使用 cancel_all；删除、暂停、恢复、立即执行分别使用 cancel、pause、resume、run_now。
2. 管理单个任务时提取 task_id；list 和 history 可提取 page。缺少必需的任务编号时 action=clarify，只询问编号。
3. 创建任务时从原始请求中提取执行时间和任务目标，不要生成代码、SQL 或 MCP 工具名。
4. daily 使用 24 小时制 HH:MM；一天多个时间使用 daily_multi，并把所有 HH:MM 放入 daily_times；once 使用 YYYY-MM-DD HH:MM；interval 使用整数分钟。
5. 需要届时查询、分析或汇总业务系统真实数据的任务使用 agent。只发送固定提醒文字的任务使用 reminder。
6. “查询预计日期前三天仍未完成并通知我”属于 agent，而“提醒我提交日报”属于 reminder。
7. prompt 保存届时交给 Agent 或提醒器的完整目标，删除“创建定时任务”“开始执行内容是”等外层措辞。
8. 如果用户明确补充了 Agent/提醒模式，以补充内容为准。
9. 缺少执行时间或任务内容时 action=clarify，并在 clarification 中只询问缺少的信息；不要猜测。
10. original_request 是引用回复链中的原始创建请求，follow_up 是用户当前补充。两者存在时合并理解。
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
            return _normalize_schedule_arguments(arguments)
    content = getattr(response, "content", "")
    if isinstance(content, str):
        match = TEXT_TOOL_CALL_PATTERN.search(content)
        if match:
            arguments = {}
            for name, raw_value in TEXT_TOOL_PARAMETER_PATTERN.findall(match.group(1)):
                if name not in SCHEDULE_PLAN_FIELDS:
                    continue
                value = unescape(raw_value.strip())
                if value.casefold() in {"", "null", "none", "nil"}:
                    arguments[name] = None
                elif name == "daily_times":
                    try:
                        arguments[name] = json.loads(value)
                    except json.JSONDecodeError:
                        arguments[name] = [
                            item.strip() for item in value.split(",") if item.strip()
                        ]
                else:
                    arguments[name] = value
            if arguments:
                return _normalize_schedule_arguments(arguments)
    raise SchedulePlanError("模型没有调用定时任务规划工具，请重试")


def _normalize_schedule_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    daily_times = arguments.get("daily_times")
    if not isinstance(daily_times, str):
        return arguments
    try:
        parsed = json.loads(daily_times)
    except json.JSONDecodeError:
        parsed = [item.strip() for item in daily_times.split(",") if item.strip()]
    return {**arguments, "daily_times": parsed}
