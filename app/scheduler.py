from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime
from typing import Any

from app import scheduler_runtime
from app.db import execute, fetch_all, fetch_one, open_db
from app.feishu import TenantApp
from app.memory.repository import fetch_recent_turns
from app.schedule_planner import SchedulePlan, SchedulePlanError, plan_schedule_creation
from app.settings import get_settings


INPUT_DATETIME_FORMAT = "%Y-%m-%d %H:%M"
DB_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
logger = logging.getLogger("feishu_scheduler")
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 7 * 24 * 60
PREVIOUS_QUERY_PROMPT = "__previous_business_query__"


def parse_schedule_command(text: str) -> dict[str, str] | None:
    text = re.sub(r"[。！？!?]+$", "", (text or "").strip()).strip()
    explicit_name, text = extract_outer_task_name(text)
    explicit_mode, text = extract_execution_mode(text)
    list_match = re.match(
        r"^(?:查看|列出|显示)?\s*(?:(?:我的|我创建的|全部)\s*)?"
        r"定时任务(?:列表)?(?:\s*第?\s*(\d+)\s*页)?$",
        text,
    )
    if list_match:
        return {
            "schedule_type": "list",
            "page": list_match.group(1) or "1",
        }

    history = re.match(
        r"^(?:查看)?\s*(?:定时任务\s*)?(?:#|第)?\s*(\d+)\s*(?:号|个)?"
        r"\s*(?:定时任务)?\s*(?:的)?\s*(?:运行记录|执行记录|执行历史|历史)"
        r"(?:\s*第?\s*(\d+)\s*页)?$",
        text,
    )
    if history:
        return {
            "schedule_type": "history",
            "task_id": history.group(1),
            "page": history.group(2) or "1",
        }

    management_patterns = (
        (
            "cancel",
            r"^(?:取消|删除|删掉|移除)\s*(?:定时任务\s*)?(?:#|第)?\s*(\d+)"
            r"\s*(?:号|个)?\s*(?:定时任务)?$",
        ),
        (
            "pause",
            r"^(?:暂停|停用|停止|关掉)\s*(?:定时任务\s*)?(?:#|第)?\s*(\d+)"
            r"\s*(?:号|个)?\s*(?:定时任务)?$",
        ),
        (
            "resume",
            r"^(?:恢复|启用|开启|打开)\s*(?:定时任务\s*)?(?:#|第)?\s*(\d+)"
            r"\s*(?:号|个)?\s*(?:定时任务)?$",
        ),
        (
            "run_now",
            r"^(?:立即执行|马上执行|执行|运行)\s*(?:定时任务\s*)?"
            r"(?:#|第)?\s*(\d+)"
            r"\s*(?:号|个)?\s*(?:定时任务)?$",
        ),
    )
    for action, pattern in management_patterns:
        match = re.match(pattern, text)
        if match:
            return {"schedule_type": action, "task_id": match.group(1)}

    once = re.match(
        r"^(?:定时|在)\s*(\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2})\s*(.+)$",
        text,
        re.S,
    )
    if once:
        try:
            run_at = normalize_datetime(once.group(1))
        except ValueError:
            return invalid_time_command("日期时间无效，请使用例如 2026-08-16 18:30。")
        prompt, inline_name = extract_inline_task_name(once.group(2).strip())
        command = {"schedule_type": "once", "run_at": run_at, "prompt": prompt}
        task_name = explicit_name or inline_name
        if task_name:
            command["task_name"] = task_name
        if explicit_mode:
            command["execution_mode"] = explicit_mode
        return command

    daily = re.match(r"^每天\s*(\d{1,2}:\d{2})\s*(.+)$", text, re.S)
    if daily:
        try:
            daily_time = normalize_time(daily.group(1))
        except ValueError:
            return invalid_time_command("时间无效，请使用 00:00 到 23:59，例如 09:00。")
        prompt, inline_name = extract_inline_task_name(daily.group(2).strip())
        command = {
            "schedule_type": "daily",
            "daily_time": daily_time,
            "prompt": prompt,
        }
        task_name = explicit_name or inline_name
        if task_name:
            command["task_name"] = task_name
        if explicit_mode:
            command["execution_mode"] = explicit_mode
        return command

    natural_daily = re.match(
        r"^每天\s*(早上|上午|中午|下午|晚上)?\s*(\d{1,2})\s*点"
        r"\s*(?:(半)|(\d{1,2})\s*分?)?\s*(.+)$",
        text,
        re.S,
    )
    if natural_daily:
        period = natural_daily.group(1) or ""
        hour = int(natural_daily.group(2))
        minute = 30 if natural_daily.group(3) else int(natural_daily.group(4) or 0)
        if period in {"下午", "晚上"} and 1 <= hour < 12:
            hour += 12
        elif period in {"早上", "上午"} and hour == 12:
            hour = 0
        try:
            daily_time = normalize_time(f"{hour}:{minute:02d}")
        except ValueError:
            return invalid_time_command("时间无效，例如可发送“每天早上9点提醒我”。")
        prompt, inline_name = extract_inline_task_name(natural_daily.group(5).strip())
        command = {
            "schedule_type": "daily",
            "daily_time": daily_time,
            "prompt": prompt,
        }
        task_name = explicit_name or inline_name
        if task_name:
            command["task_name"] = task_name
        if explicit_mode:
            command["execution_mode"] = explicit_mode
        return command

    relative_once = re.match(
        r"^([零〇一二两三四五六七八九十百半\d]+)\s*(分钟|小时|天)后\s*(.+)$",
        text,
        re.S,
    )
    if relative_once:
        delay_minutes = parse_duration_minutes(
            relative_once.group(1), relative_once.group(2)
        )
        if not 1 <= delay_minutes <= MAX_INTERVAL_MINUTES:
            return invalid_time_command("延迟时间必须在 1 分钟到 7 天之间。")
        prompt, inline_name = extract_inline_task_name(relative_once.group(3).strip())
        command = {
            "schedule_type": "once",
            "run_at": scheduler_runtime.next_interval_run(delay_minutes),
            "prompt": prompt,
        }
        task_name = explicit_name or inline_name
        if task_name:
            command["task_name"] = task_name
        if explicit_mode:
            command["execution_mode"] = explicit_mode
        return command

    interval = re.match(
        r"^(?:每隔|每)\s*([零〇一二两三四五六七八九十百半\d]+)\s*"
        r"(分钟|小时|天)(?:后)?\s*(.*)$",
        text,
        re.S,
    )
    if interval:
        interval_minutes = parse_duration_minutes(interval.group(1), interval.group(2))
        if not MIN_INTERVAL_MINUTES <= interval_minutes <= MAX_INTERVAL_MINUTES:
            return invalid_time_command(
                f"间隔时间必须在 {MIN_INTERVAL_MINUTES} 分钟到 7 天之间。"
            )
        raw_prompt = interval.group(3).strip(" ，,。；;：:")
        use_previous = raw_prompt in {"", "提醒", "提醒我", "执行", "运行", "查询"}
        prompt = PREVIOUS_QUERY_PROMPT if use_previous else raw_prompt
        prompt, inline_name = extract_inline_task_name(prompt)
        command = {
            "schedule_type": "interval",
            "interval_minutes": str(interval_minutes),
            "prompt": prompt,
        }
        task_name = explicit_name or inline_name
        if task_name:
            command["task_name"] = task_name
        if explicit_mode:
            command["execution_mode"] = explicit_mode
        return command

    if is_schedule_creation_intent(text):
        return {"schedule_type": "help"}
    if is_schedule_management_intent(text):
        return invalid_time_command(
            "没有识别出任务编号或操作，请使用例如“删除#1定时任务”。"
        )
    return None


async def resolve_schedule_command(
    text: str,
    *,
    referenced_message_text: str | None = None,
    referenced_request_text: str | None = None,
) -> dict[str, str] | None:
    command = parse_schedule_command(text)
    if command and command.get("schedule_type") != "help":
        return command

    original_request = referenced_request_text or ""
    if not original_request and referenced_message_text and is_schedule_creation_intent(
        referenced_message_text
    ):
        original_request = referenced_message_text
    should_plan = bool(
        (command and command.get("schedule_type") == "help")
        or (original_request and is_execution_mode_reply(text))
    )
    if not should_plan:
        return command

    try:
        plan = await plan_schedule_creation(
            text,
            original_request=original_request or None,
        )
        return schedule_plan_to_command(plan)
    except SchedulePlanError as exc:
        return invalid_time_command(str(exc))


def schedule_plan_to_command(plan: SchedulePlan) -> dict[str, str]:
    if plan.action == "clarify":
        return invalid_time_command(
            (plan.clarification or "请补充执行时间和任务内容。").strip()
        )
    prompt = plan.prompt.strip()
    if not prompt:
        return invalid_time_command("缺少定时任务的执行内容。")
    if not plan.schedule_type:
        return invalid_time_command("缺少定时任务的执行时间。")

    command: dict[str, str] = {
        "schedule_type": plan.schedule_type,
        "prompt": prompt,
    }
    if plan.execution_mode:
        command["execution_mode"] = plan.execution_mode
    if plan.task_name and plan.task_name.strip():
        command["task_name"] = plan.task_name.strip()

    try:
        if plan.schedule_type == "daily":
            if not plan.daily_time:
                raise ValueError
            command["daily_time"] = normalize_time(plan.daily_time.replace("：", ":"))
        elif plan.schedule_type == "once":
            if not plan.run_at:
                raise ValueError
            command["run_at"] = normalize_datetime(plan.run_at.replace("：", ":"))
        else:
            interval_minutes = int(plan.interval_minutes or 0)
            if not MIN_INTERVAL_MINUTES <= interval_minutes <= MAX_INTERVAL_MINUTES:
                return invalid_time_command(
                    f"间隔时间必须在 {MIN_INTERVAL_MINUTES} 分钟到 7 天之间。"
                )
            command["interval_minutes"] = str(interval_minutes)
    except (TypeError, ValueError):
        return invalid_time_command("模型识别出的执行时间无效，请换一种时间表达重试。")
    return command


def is_execution_mode_reply(text: str) -> bool:
    normalized = re.sub(r"[\s，,。.!！]+", "", (text or "")).casefold()
    return normalized in {"agent", "agent模式", "提醒", "提醒模式"}


def extract_outer_task_name(text: str) -> tuple[str | None, str]:
    quoted = re.match(
        r'^(?:创建|新建|设置)(?:一个)?(?:名为|名称为)\s*[“"]([^”"]+)[”"]'
        r"\s*的?定时任务\s*[：:,，]\s*(.+)$",
        text,
        re.S,
    )
    if quoted:
        return quoted.group(1).strip(), quoted.group(2).strip()
    plain = re.match(
        r"^(?:创建|新建|设置)(?:一个)?(?:名为|名称为)\s*([^：:,，]{1,40}?)"
        r"\s*的?定时任务\s*[：:,，]\s*(.+)$",
        text,
        re.S,
    )
    if plain:
        return plain.group(1).strip(), plain.group(2).strip()
    return None, text


def extract_execution_mode(text: str) -> tuple[str | None, str]:
    match = re.match(
        r"^(Agent|提醒)\s*(?:模式)+\s*[：:,，]?\s*(.+)$",
        text,
        re.I | re.S,
    )
    if not match:
        return None, text
    mode = "agent" if match.group(1).lower() == "agent" else "reminder"
    return mode, match.group(2).strip()


def parse_interval_number(value: str) -> int | None:
    value = value.strip()
    if value.isdigit():
        return int(value)
    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if "百" in value:
        left, right = value.split("百", 1)
        hundreds = digits.get(left or "一")
        remainder = parse_interval_number(right) if right else 0
        return None if hundreds is None or remainder is None else hundreds * 100 + remainder
    if "十" in value:
        left, right = value.split("十", 1)
        tens = digits.get(left or "一")
        ones = digits.get(right, 0) if right else 0
        return None if tens is None or ones is None else tens * 10 + ones
    return digits.get(value)


def parse_duration_minutes(value: str, unit: str) -> int:
    if value == "半":
        return {"分钟": 0, "小时": 30, "天": 12 * 60}.get(unit, 0)
    amount = parse_interval_number(value)
    multiplier = {"分钟": 1, "小时": 60, "天": 24 * 60}.get(unit, 0)
    return amount * multiplier if amount else 0


def extract_inline_task_name(prompt: str) -> tuple[str, str | None]:
    prefix = re.match(
        r"^(?:任务名称|名称)\s*[：:]\s*([^；;,，]+)[；;,，]\s*(.+)$",
        prompt,
        re.S,
    )
    if prefix:
        return prefix.group(2).strip(), prefix.group(1).strip()
    suffix = re.match(
        r"^(.+?)[；;,，]\s*(?:任务名称|名称)\s*[：:]\s*(.+)$",
        prompt,
        re.S,
    )
    if suffix:
        return suffix.group(1).strip(), suffix.group(2).strip()
    return prompt, None


def is_schedule_creation_intent(text: str) -> bool:
    creation_markers = ("创建", "新建", "设置", "新增", "创个", "建个", "怎么建", "怎么创建")
    return (
        "定时任务" in text and any(marker in text for marker in creation_markers)
    ) or ("提醒" in text and any(marker in text for marker in creation_markers))


def is_schedule_management_intent(text: str) -> bool:
    markers = (
        "查看",
        "列出",
        "显示",
        "删除",
        "删掉",
        "移除",
        "取消",
        "暂停",
        "停用",
        "停止",
        "关掉",
        "恢复",
        "启用",
        "开启",
        "打开",
        "立即执行",
        "马上执行",
        "执行",
        "运行",
        "运行记录",
        "执行记录",
        "执行历史",
    )
    return "定时任务" in text and any(marker in text for marker in markers)


def invalid_time_command(message: str) -> dict[str, str]:
    return {"schedule_type": "invalid", "error": message}


async def handle_schedule_command(app: TenantApp, event: dict[str, Any], command: dict[str, str]) -> str:
    if command["schedule_type"] == "help":
        return schedule_help_text()
    if command["schedule_type"] == "invalid":
        return command["error"] + "\n\n" + schedule_help_text()
    if command["schedule_type"] == "list":
        return await list_scheduled_tasks(app, event, int(command.get("page") or 1))
    if command["schedule_type"] in {"cancel", "pause", "resume", "run_now"}:
        return await manage_scheduled_task(
            app, event, int(command["task_id"]), command["schedule_type"]
        )
    if command["schedule_type"] == "history":
        return await scheduled_task_history(
            app,
            event,
            int(command["task_id"]),
            int(command.get("page") or 1),
        )
    return await create_scheduled_task(app, event, command)


def schedule_help_text() -> str:
    return (
        "支持两种定时任务：\n"
        "- 提醒模式：到时间发送提醒文字。\n"
        "- Agent 模式：到时间自动查询业务系统并推送真实结果。\n\n"
        "创建示例：\n"
        "- 定时 2026-08-16 18:30 提醒我提交日报\n"
        "- 30分钟后提醒我参加会议\n"
        "- 每天 09:00 提醒我检查生产进度\n"
        "- 每天 09:00 查询样品风险并汇总\n\n"
        "- Agent模式，每5分钟查询样品风险并汇总\n"
        "- 每隔2小时提醒我检查待办\n\n"
        "管理命令：\n"
        "- 查看定时任务\n"
        "- 暂停定时任务 #任务编号\n"
        "- 恢复定时任务 #任务编号\n"
        "- 立即执行定时任务 #任务编号\n"
        "- 查看定时任务 #任务编号 运行记录\n"
        "- 取消定时任务 #任务编号"
    )


def execution_mode_for_prompt(prompt: str) -> str:
    if re.match(r"^(?:请)?提醒(?:我)?", prompt.strip()):
        return "reminder"
    return "agent"


def auto_task_name(prompt: str, execution_mode: str) -> str:
    name = " ".join(prompt.strip().split())
    name = re.sub(r"^(?:请|帮我|自动)", "", name).strip()
    if execution_mode == "reminder":
        name = re.sub(r"^提醒(?:我)?", "", name).strip()
        if name and not name.endswith("提醒"):
            name += "提醒"
    else:
        name = re.sub(r"^(?:查询|获取|分析)", "", name).strip()
        name = name.replace("并汇总", "汇总")
    name = name.strip(" ，,。；;：:") or (
        "定时提醒" if execution_mode == "reminder" else "定时业务查询"
    )
    return name[:24]


def interval_schedule_text(interval_minutes: int) -> str:
    if interval_minutes % (24 * 60) == 0:
        return f"每 {interval_minutes // (24 * 60)} 天"
    if interval_minutes % 60 == 0:
        return f"每 {interval_minutes // 60} 小时"
    return f"每 {interval_minutes} 分钟"


async def previous_business_query(event: dict[str, Any]) -> str | None:
    session_id = str(event.get("_session_id") or "")
    if not session_id:
        return None
    turns = await fetch_recent_turns(
        tenant_key=event["tenant_key"],
        app_id=event["app_id"],
        open_id=event["open_id"],
        chat_id=event.get("chat_id"),
        session_id=session_id,
        limit=20,
    )
    for turn in reversed(turns):
        if turn.get("role") != "user":
            continue
        content = str(turn.get("content") or "").strip()
        if not content or content == "/new" or parse_schedule_command(content):
            continue
        return content
    return None


async def recent_schedule_creation_request(event: dict[str, Any]) -> str | None:
    """Return only the immediately preceding schedule request awaiting follow-up."""
    session_id = str(event.get("_session_id") or "")
    if not session_id:
        return None
    turns = await fetch_recent_turns(
        tenant_key=event["tenant_key"],
        app_id=event["app_id"],
        open_id=event["open_id"],
        chat_id=event.get("chat_id"),
        session_id=session_id,
        limit=6,
    )
    latest_user_index = next(
        (
            index
            for index in range(len(turns) - 1, -1, -1)
            if turns[index].get("role") == "user"
        ),
        None,
    )
    if latest_user_index is None:
        return None
    request = str(turns[latest_user_index].get("content") or "").strip()
    if not request or not is_schedule_creation_intent(request):
        return None
    later_answers = turns[latest_user_index + 1 :]
    if any(
        turn.get("role") == "assistant"
        and "已创建定时任务" in str(turn.get("content") or "")
        for turn in later_answers
    ):
        return None
    return request


def normalize_explicit_task_name(value: str) -> str:
    return " ".join((value or "").strip().split())


async def task_name_exists(
    app: TenantApp, event: dict[str, Any], task_name: str
) -> bool:
    row = await fetch_one(
        """
        SELECT id FROM scheduled_task
        WHERE tenant_key = ? AND app_id = ? AND chat_id = ? AND open_id = ?
          AND task_name = ?
        LIMIT 1
        """,
        (
            event["tenant_key"],
            app.app_id,
            event["chat_id"],
            event["open_id"],
            task_name,
        ),
    )
    return row is not None


async def unique_generated_task_name(
    app: TenantApp, event: dict[str, Any], base_name: str
) -> str:
    if not await task_name_exists(app, event, base_name):
        return base_name
    number = 2
    while number <= 999:
        suffix = f"（{number}）"
        candidate = f"{base_name[: 40 - len(suffix)]}{suffix}"
        if not await task_name_exists(app, event, candidate):
            return candidate
        number += 1
    return f"任务-{uuid.uuid4().hex[:8]}"


async def create_scheduled_task(app: TenantApp, event: dict[str, Any], command: dict[str, str]) -> str:
    settings = get_settings()
    prompt = command["prompt"]
    if prompt == PREVIOUS_QUERY_PROMPT:
        prompt = await previous_business_query(event)
        if not prompt:
            return "没有找到可以继承的上一条业务问题，请在定时命令中写明要执行的内容。"
    execution_mode = command.get("execution_mode") or execution_mode_for_prompt(prompt)
    explicit_name = normalize_explicit_task_name(command.get("task_name") or "")
    if explicit_name:
        if len(explicit_name) > 40:
            return "任务名称不能超过 40 个字符，请使用更短的名称。"
        if await task_name_exists(app, event, explicit_name):
            return f"任务名称“{explicit_name}”已经存在，请换一个名称。"
        task_name = explicit_name
    else:
        task_name = await unique_generated_task_name(
            app,
            event,
            auto_task_name(prompt, execution_mode),
        )
    next_run_at = command.get("run_at")
    if command["schedule_type"] == "daily":
        next_run_at = scheduler_runtime.next_daily_run(command["daily_time"])
    elif command["schedule_type"] == "interval":
        next_run_at = scheduler_runtime.next_interval_run(
            int(command["interval_minutes"])
        )

    async with open_db() as db:
        cur = await db.execute(
            """
            INSERT INTO scheduled_task (
              task_name, tenant_key, app_id, bot_code, chat_id, chat_type, open_id,
              schedule_type, run_at, daily_time, interval_minutes, prompt, next_run_at,
              execution_mode, timeout_seconds, max_retries, timezone, last_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'idle')
            """,
            (
                task_name,
                event["tenant_key"],
                app.app_id,
                app.bot_code,
                event["chat_id"],
                event.get("chat_type"),
                event["open_id"],
                command["schedule_type"],
                command.get("run_at"),
                command.get("daily_time"),
                command.get("interval_minutes"),
                prompt,
                next_run_at,
                execution_mode,
                settings.scheduler_task_timeout_seconds,
                settings.scheduler_max_retries,
                scheduler_runtime.DEFAULT_TIMEZONE,
            ),
        )
        task_id = int(cur.lastrowid)
    mode_label = "自动执行 Agent 查询" if execution_mode == "agent" else "发送提醒"
    if command["schedule_type"] == "daily":
        schedule = f"每天 {command['daily_time']}"
    elif command["schedule_type"] == "interval":
        schedule = interval_schedule_text(int(command["interval_minutes"]))
    else:
        schedule = scheduler_runtime.display_datetime(command["run_at"])
    return (
        f"已创建定时任务 #{task_id}\n"
        f"- 任务名称：{task_name}\n"
        f"- 执行时间：{schedule}\n"
        f"- 时区：{scheduler_runtime.DEFAULT_TIMEZONE}\n"
        f"- 执行模式：{mode_label}\n"
        f"- 任务内容：{prompt}\n"
        f"- 下次执行：{scheduler_runtime.display_datetime(next_run_at)}\n"
        f"- 超时：{settings.scheduler_task_timeout_seconds} 秒\n"
        f"- 失败重试：最多 {settings.scheduler_max_retries} 次"
    )


async def list_scheduled_tasks(
    app: TenantApp, event: dict[str, Any], page: int = 1
) -> str:
    settings = get_settings()
    page_size = min(max(settings.scheduler_list_page_size, 1), 20)
    page = max(page, 1)
    scope = (event["tenant_key"], app.app_id, event["chat_id"], event["open_id"])
    count_row = await fetch_one(
        """
        SELECT COUNT(*) AS total
        FROM scheduled_task
        WHERE tenant_key = ? AND app_id = ? AND chat_id = ? AND open_id = ?
          AND (last_status IS NULL OR last_status != 'cancelled')
        """,
        scope,
    )
    total = int((count_row or {}).get("total") or 0)
    if total == 0:
        return "当前没有定时任务。\n\n" + schedule_help_text()
    total_pages = (total + page_size - 1) // page_size
    if page > total_pages:
        return (
            f"页码超出范围：当前共有 {total} 个定时任务、{total_pages} 页。\n"
            f"请发送“查看定时任务 第{total_pages}页”。"
        )

    rows = await fetch_all(
        """
        SELECT id, task_name, schedule_type, run_at, daily_time, interval_minutes,
               prompt, next_run_at,
               execution_mode, last_status, consecutive_failures
        FROM scheduled_task
        WHERE tenant_key = ?
          AND app_id = ?
          AND chat_id = ?
          AND open_id = ?
          AND (last_status IS NULL OR last_status != 'cancelled')
        ORDER BY next_run_at, id
        LIMIT ? OFFSET ?
        """,
        (*scope, page_size, (page - 1) * page_size),
    )

    lines = [f"当前定时任务（第 {page}/{total_pages} 页，共 {total} 个）："]
    for row in rows:
        if row["schedule_type"] == "daily":
            schedule = f"每天 {row['daily_time']}"
        elif row["schedule_type"] == "interval":
            schedule = interval_schedule_text(int(row["interval_minutes"]))
        else:
            schedule = scheduler_runtime.display_datetime(row["run_at"])
        mode = "Agent" if row["execution_mode"] == "agent" else "提醒"
        status = row["last_status"] or "idle"
        failure = (
            f"，连续失败 {row['consecutive_failures']} 次"
            if row["consecutive_failures"]
            else ""
        )
        lines.append(
            f"- #{row['id']} {row['task_name'] or '未命名任务'} [{mode}/{status}] "
            f"{schedule}：{row['prompt']}"
            f"（下次：{scheduler_runtime.display_datetime(row['next_run_at'])}{failure}）"
        )
    append_page_commands(
        lines,
        page=page,
        total_pages=total_pages,
        command=lambda target: f"查看定时任务 第{target}页",
    )
    lines.append("\n发送“查看定时任务 #编号 运行记录”可查看执行历史。")
    return "\n".join(lines)


async def scoped_task(
    app: TenantApp, event: dict[str, Any], task_id: int
) -> dict[str, Any] | None:
    return await fetch_one(
        """
        SELECT * FROM scheduled_task
        WHERE id = ? AND tenant_key = ? AND app_id = ? AND chat_id = ? AND open_id = ?
        """,
        (task_id, event["tenant_key"], app.app_id, event["chat_id"], event["open_id"]),
    )


async def manage_scheduled_task(
    app: TenantApp,
    event: dict[str, Any],
    task_id: int,
    action: str,
) -> str:
    task = await scoped_task(app, event, task_id)
    if not task:
        return f"没有找到定时任务 #{task_id}。"

    if action in {"cancel", "pause"}:
        status = "cancelled" if action == "cancel" else "paused"
        await execute(
            """
            UPDATE scheduled_task
            SET enabled = 0, locked_until = NULL, last_status = ?
            WHERE id = ?
            """,
            (status, task_id),
        )
        verb = "取消" if action == "cancel" else "暂停"
        return f"已{verb}定时任务 #{task_id}“{task['task_name'] or '未命名任务'}”。"

    if action == "resume":
        next_run_at = task["run_at"]
        if task["schedule_type"] == "daily":
            next_run_at = scheduler_runtime.next_daily_run(task["daily_time"])
        elif task["schedule_type"] == "interval":
            next_run_at = scheduler_runtime.next_interval_run(
                int(task["interval_minutes"])
            )
        elif not next_run_at or next_run_at <= scheduler_runtime.now_text():
            next_run_at = scheduler_runtime.now_text()
        await execute(
            """
            UPDATE scheduled_task
            SET enabled = 1, next_run_at = ?, locked_until = NULL,
                consecutive_failures = 0, last_error = NULL, last_status = 'idle'
            WHERE id = ?
            """,
            (next_run_at, task_id),
        )
        return (
            f"已恢复定时任务 #{task_id}“{task['task_name'] or '未命名任务'}”，下次执行："
            f"{scheduler_runtime.display_datetime(next_run_at)}。"
        )

    await execute(
        """
        UPDATE scheduled_task
        SET enabled = 1, next_run_at = ?, locked_until = NULL, last_status = 'queued'
        WHERE id = ?
        """,
        (scheduler_runtime.now_text(), task_id),
    )
    return (
        f"定时任务 #{task_id}“{task['task_name'] or '未命名任务'}”已加入执行队列。"
    )


async def scheduled_task_history(
    app: TenantApp, event: dict[str, Any], task_id: int, page: int = 1
) -> str:
    task = await scoped_task(app, event, task_id)
    if not task:
        return f"没有找到定时任务 #{task_id}。"
    settings = get_settings()
    page_size = min(max(settings.scheduler_history_page_size, 1), 20)
    page = max(page, 1)
    count_row = await fetch_one(
        "SELECT COUNT(*) AS total FROM scheduled_task_run WHERE task_id = ?",
        (task_id,),
    )
    total = int((count_row or {}).get("total") or 0)
    if total == 0:
        return f"定时任务 #{task_id}“{task['task_name'] or '未命名任务'}”还没有运行记录。"
    total_pages = (total + page_size - 1) // page_size
    if page > total_pages:
        return (
            f"页码超出范围：任务 #{task_id} 共有 {total} 条运行记录、{total_pages} 页。\n"
            f"请发送“查看定时任务 #{task_id} 运行记录 第{total_pages}页”。"
        )

    rows = await fetch_all(
        """
        SELECT run_id, status, attempt, started_at, finished_at, error
        FROM scheduled_task_run
        WHERE task_id = ?
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
        (task_id, page_size, (page - 1) * page_size),
    )
    lines = [
        f"定时任务 #{task_id}“{task['task_name'] or '未命名任务'}”运行记录"
        f"（第 {page}/{total_pages} 页，共 {total} 条）："
    ]
    for row in rows:
        detail = f"，错误：{row['error']}" if row["error"] else ""
        lines.append(
            f"- {row['started_at']} [{row['status']}] "
            f"第 {row['attempt']} 次，run_id={row['run_id'][:8]}{detail}"
        )
    append_page_commands(
        lines,
        page=page,
        total_pages=total_pages,
        command=lambda target: f"查看定时任务 #{task_id} 运行记录 第{target}页",
    )
    return "\n".join(lines)


def append_page_commands(
    lines: list[str],
    *,
    page: int,
    total_pages: int,
    command,
) -> None:
    commands: list[str] = []
    if page > 1:
        commands.append(f"上一页：{command(page - 1)}")
    if page < total_pages:
        commands.append(f"下一页：{command(page + 1)}")
    if commands:
        lines.extend(["", *commands])


def start_scheduler_thread(apps: list[TenantApp]) -> None:
    import threading

    thread = threading.Thread(
        target=lambda: asyncio.run(scheduler_runtime.run_scheduler(list(apps))),
        name="feishu-scheduler",
        daemon=True,
    )
    thread.start()


async def run_due_tasks(app_by_id: dict[str, TenantApp]) -> None:
    await scheduler_runtime.run_due_tasks(app_by_id)


def normalize_datetime(value: str) -> str:
    parsed = datetime.strptime(value.strip(), INPUT_DATETIME_FORMAT)
    return parsed.strftime(DB_DATETIME_FORMAT)


def normalize_time(value: str) -> str:
    return datetime.strptime(value.strip(), "%H:%M").strftime("%H:%M")


def next_daily_run(daily_time: str) -> str:
    return scheduler_runtime.next_daily_run(daily_time)
