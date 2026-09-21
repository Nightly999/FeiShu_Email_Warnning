from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app.schedule_planner import SchedulePlan, SchedulePlanError
from app.scheduler import (
    PREVIOUS_QUERY_PROMPT,
    auto_task_name,
    execution_mode_for_prompt,
    parse_schedule_command,
    recent_schedule_creation_request,
    resolve_schedule_command,
    schedule_help_text,
)


class ScheduleCommandTests(unittest.TestCase):
    def test_generic_creation_request_opens_help(self) -> None:
        command = parse_schedule_command("帮我创个定时任务")
        self.assertEqual(command, {"schedule_type": "help"})
        self.assertIn("每天 09:00", schedule_help_text())
        self.assertIn("邮箱定时分析示例", schedule_help_text())
        self.assertNotIn("生产进度", schedule_help_text())

    def test_daily_task_is_parsed(self) -> None:
        command = parse_schedule_command("每天 9:05 提醒我查看样品风险")
        self.assertEqual(
            command,
            {
                "schedule_type": "daily",
                "daily_time": "09:05",
                "prompt": "提醒我查看样品风险",
            },
        )

    def test_multiple_daily_email_times_are_parsed(self) -> None:
        command = parse_schedule_command("每天9:30和17:20推送当天重要邮件")
        self.assertEqual(
            command,
            {
                "schedule_type": "daily_multi",
                "daily_times": "09:30,17:20",
                "prompt": "推送当天重要邮件",
            },
        )

    def test_prefixed_chinese_daily_email_times_are_parsed(self) -> None:
        command = parse_schedule_command("定时每天十点和晚上九点推送，处理未读邮件")
        self.assertEqual(
            command,
            {
                "schedule_type": "daily_multi",
                "daily_times": "10:00,21:00",
                "prompt": "推送，处理未读邮件",
            },
        )

    def test_fullwidth_mixed_period_email_times_are_parsed(self) -> None:
        command = parse_schedule_command("定时每天8：20和晚上5：20推送，处理未读邮件")
        self.assertEqual(
            command,
            {
                "schedule_type": "daily_multi",
                "daily_times": "08:20,17:20",
                "prompt": "推送，处理未读邮件",
            },
        )

    def test_natural_one_time_task_is_parsed(self) -> None:
        command = parse_schedule_command("在 2026-08-16 18:30 提醒我提交日报")
        self.assertEqual(
            command,
            {
                "schedule_type": "once",
                "run_at": "2026-08-16 18:30:00",
                "prompt": "提醒我提交日报",
            },
        )

    def test_invalid_time_returns_user_facing_error(self) -> None:
        command = parse_schedule_command("每天 29:70 提醒我提交日报")
        self.assertEqual(command["schedule_type"], "invalid")
        self.assertIn("时间无效", command["error"])

    def test_management_commands_are_parsed(self) -> None:
        self.assertEqual(
            parse_schedule_command("立即执行定时任务 #12"),
            {"schedule_type": "run_now", "task_id": "12"},
        )
        self.assertEqual(
            parse_schedule_command("查看定时任务 #12 运行记录"),
            {"schedule_type": "history", "task_id": "12", "page": "1"},
        )

    def test_management_commands_accept_natural_word_order(self) -> None:
        cases = {
            "删除#1定时任务": "cancel",
            "删除第1个定时任务": "cancel",
            "删掉#1定时任务": "cancel",
            "移除1号定时任务": "cancel",
            "暂停#2定时任务": "pause",
            "停用第2个定时任务": "pause",
            "关掉#2定时任务": "pause",
            "恢复第3号定时任务": "resume",
            "启用#3定时任务": "resume",
            "打开第3个定时任务": "resume",
            "立即执行#4定时任务": "run_now",
            "执行第4个定时任务": "run_now",
        }
        for text, action in cases.items():
            with self.subTest(text=text):
                command = parse_schedule_command(text)
                self.assertEqual(command["schedule_type"], action)

    def test_list_and_history_page_are_parsed(self) -> None:
        self.assertEqual(
            parse_schedule_command("查看定时任务 第2页"),
            {"schedule_type": "list", "page": "2"},
        )
        self.assertEqual(
            parse_schedule_command("查看定时任务 #12 运行记录 第3页"),
            {"schedule_type": "history", "task_id": "12", "page": "3"},
        )
        self.assertEqual(
            parse_schedule_command("查看我创建的定时任务列表"),
            {"schedule_type": "list", "page": "1"},
        )
        self.assertEqual(
            parse_schedule_command("查看第12个定时任务的执行历史"),
            {"schedule_type": "history", "task_id": "12", "page": "1"},
        )

    def test_relative_one_time_task_is_parsed(self) -> None:
        command = parse_schedule_command("十五分钟后提醒我参加会议")
        self.assertEqual(command["schedule_type"], "once")
        self.assertEqual(command["prompt"], "提醒我参加会议")
        self.assertRegex(command["run_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        half_hour = parse_schedule_command("半小时后提醒我休息")
        self.assertEqual(half_hour["schedule_type"], "once")

    def test_natural_daily_time_is_parsed(self) -> None:
        morning = parse_schedule_command("每天早上9点提醒我提交日报")
        afternoon = parse_schedule_command("每天下午3点半查询样品风险")
        noon = parse_schedule_command("每天中午1点分析邮件")
        midnight = parse_schedule_command("每天凌晨12点分析邮件")
        self.assertEqual(morning["daily_time"], "09:00")
        self.assertEqual(afternoon["daily_time"], "15:30")
        self.assertEqual(noon["daily_time"], "13:00")
        self.assertEqual(midnight["daily_time"], "00:00")

    def test_cancel_without_id_cancels_all_current_user_tasks(self) -> None:
        for text in ("取消定时任务", "删除定时任务", "取消全部定时任务"):
            with self.subTest(text=text):
                self.assertEqual(
                    parse_schedule_command(text),
                    {"schedule_type": "cancel_all"},
                )

    def test_prompt_selects_reminder_or_agent_mode(self) -> None:
        self.assertEqual(execution_mode_for_prompt("提醒我提交日报"), "reminder")
        self.assertEqual(execution_mode_for_prompt("查询样品风险并汇总"), "agent")

    def test_explicit_task_name_is_extracted(self) -> None:
        command = parse_schedule_command(
            "创建名为“每日样品风险”的定时任务：每天 09:00 查询样品风险并汇总"
        )
        self.assertEqual(command["task_name"], "每日样品风险")
        self.assertEqual(command["schedule_type"], "daily")
        self.assertEqual(command["prompt"], "查询样品风险并汇总")

    def test_inline_task_name_is_extracted(self) -> None:
        command = parse_schedule_command(
            "每天 09:00 任务名称：早间风险；查询样品风险并汇总"
        )
        self.assertEqual(command["task_name"], "早间风险")
        self.assertEqual(command["prompt"], "查询样品风险并汇总")

    def test_task_name_is_generated_from_purpose(self) -> None:
        self.assertEqual(auto_task_name("提醒我提交日报", "reminder"), "提交日报提醒")
        self.assertEqual(auto_task_name("查询样品风险并汇总", "agent"), "样品风险汇总")

    def test_interval_agent_mode_inherits_previous_query(self) -> None:
        command = parse_schedule_command("Agent 模式模式，每五分钟后提醒我")
        self.assertEqual(
            command,
            {
                "schedule_type": "interval",
                "interval_minutes": "5",
                "prompt": PREVIOUS_QUERY_PROMPT,
                "execution_mode": "agent",
            },
        )

    def test_interval_units_and_minimum_are_validated(self) -> None:
        command = parse_schedule_command("每隔2小时提醒我检查待办")
        self.assertEqual(command["interval_minutes"], "120")
        self.assertEqual(command["prompt"], "提醒我检查待办")
        half_hour = parse_schedule_command("每半小时查询样品风险")
        self.assertEqual(half_hour["interval_minutes"], "30")
        invalid = parse_schedule_command("每1分钟提醒我检查待办")
        self.assertEqual(invalid["schedule_type"], "invalid")
        self.assertIn("5 分钟", invalid["error"])


class AgentSchedulePlanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_standalone_mode_reply_uses_recent_pending_schedule_request(
        self,
    ) -> None:
        event = {
            "tenant_key": "tenant",
            "app_id": "app",
            "open_id": "user",
            "chat_id": "chat",
            "_session_id": "session",
        }
        original = "创建定时任务，每天 08:30 查询未完成工作"
        turns = [
            {"role": "user", "content": original},
            {"role": "assistant", "content": "please select Agent or reminder mode"},
        ]
        with patch(
            "app.scheduler.fetch_recent_turns",
            AsyncMock(return_value=turns),
        ):
            request = await recent_schedule_creation_request(event)

        self.assertEqual(request, original)

    async def test_natural_creation_uses_agent_plan_instead_of_help(self) -> None:
        plan = SchedulePlan(
            action="create",
            schedule_type="daily",
            daily_time="08:30",
            prompt="预计日期前三天还没有完成的通知我",
            execution_mode="agent",
        )
        with patch(
            "app.scheduler.plan_schedule_creation",
            AsyncMock(return_value=plan),
        ) as planner:
            command = await resolve_schedule_command(
                "创建定时任务 每天早上8：30开始执行内容是：预计日期前三天还没有完成的通知我"
            )

        self.assertEqual(
            command,
            {
                "schedule_type": "daily",
                "daily_time": "08:30",
                "prompt": "预计日期前三天还没有完成的通知我",
                "execution_mode": "agent",
            },
        )
        planner.assert_awaited_once()

    async def test_natural_schedule_queries_are_listed(self) -> None:
        self.assertEqual(
            parse_schedule_command("查询定时任务"),
            {"schedule_type": "list", "page": "1"},
        )
        self.assertEqual(
            parse_schedule_command("我有几个定时任务"),
            {"schedule_type": "list", "page": "1"},
        )

    def test_weekly_email_schedule_fallback(self) -> None:
        self.assertEqual(
            parse_schedule_command("每周五下午五点半分析我的邮件"),
            {
                "schedule_type": "weekly",
                "weekly_day": "4",
                "daily_time": "17:30",
                "prompt": "分析我的邮件",
            },
        )
        self.assertEqual(
            parse_schedule_command("每星期日 09:00 分析我的邮件")["weekly_day"],
            "6",
        )

    async def test_model_weekly_email_schedule(self) -> None:
        plan = SchedulePlan(
            action="create",
            schedule_type="weekly",
            weekly_day=4,
            daily_time="17:30",
            prompt="分析我的邮件",
            execution_mode="agent",
        )
        with patch(
            "app.scheduler.plan_schedule_creation", AsyncMock(return_value=plan)
        ) as planner:
            command = await resolve_schedule_command("每星期五下午五点半分析我的邮件")
        self.assertEqual(command["schedule_type"], "weekly")
        self.assertEqual(command["weekly_day"], "4")
        self.assertEqual(command["daily_time"], "17:30")
        planner.assert_awaited_once()

    async def test_weekly_time_is_not_changed_by_model(self) -> None:
        plan = SchedulePlan(
            action="create", schedule_type="daily", daily_time="17:30",
            prompt="分析我的邮件", execution_mode="agent",
        )
        with patch("app.scheduler.plan_schedule_creation", AsyncMock(return_value=plan)):
            command = await resolve_schedule_command("每周五下午五点半分析我的邮件")
        self.assertEqual(command["schedule_type"], "weekly")
        self.assertEqual(command["weekly_day"], "4")

    async def test_unrecognized_schedule_language_uses_model_plan(self) -> None:
        plan = SchedulePlan(action="list", page=2)
        with patch(
            "app.scheduler.plan_schedule_creation",
            AsyncMock(return_value=plan),
        ) as planner:
            command = await resolve_schedule_command("帮我看看之前的定时安排")

        self.assertEqual(command, {"schedule_type": "list", "page": "2"})
        planner.assert_awaited_once()

    async def test_model_plan_supports_multiple_daily_times(self) -> None:
        plan = SchedulePlan(
            action="create",
            schedule_type="daily_multi",
            daily_times=["10:00", "21:00"],
            prompt="分析并推送未读邮件",
            execution_mode="agent",
        )
        with patch(
            "app.scheduler.plan_schedule_creation",
            AsyncMock(return_value=plan),
        ):
            command = await resolve_schedule_command(
                "请设置一个每天上午十点和晚上九点发送未读邮件分析的任务"
            )

        self.assertEqual(command["schedule_type"], "daily_multi")
        self.assertEqual(command["daily_times"], "10:00,21:00")
        self.assertEqual(command["execution_mode"], "agent")

    async def test_model_is_preferred_for_parseable_schedule_creation(self) -> None:
        plan = SchedulePlan(
            action="create",
            schedule_type="daily_multi",
            daily_times=["10:00", "21:00"],
            prompt="分析最近三天的前五封未处理邮件",
            execution_mode="agent",
        )
        with patch(
            "app.scheduler.plan_schedule_creation",
            AsyncMock(return_value=plan),
        ) as planner:
            command = await resolve_schedule_command(
                "定时每天十点和晚上九点推送，分析最近三天的前五封未处理邮件"
            )

        planner.assert_awaited_once()
        self.assertEqual(command["daily_times"], "10:00,21:00")
        self.assertEqual(command["prompt"], "分析最近三天的前五封未处理邮件")

    async def test_email_schedule_uses_parser_when_model_is_temporarily_unavailable(self) -> None:
        with patch(
            "app.scheduler.plan_schedule_creation",
            AsyncMock(side_effect=SchedulePlanError("模型暂时不可用")),
        ):
            command = await resolve_schedule_command(
                "定时每天8：20和晚上5：20推送，处理未读邮件"
            )

        self.assertEqual(command["schedule_type"], "daily_multi")
        self.assertEqual(command["daily_times"], "08:20,17:20")

    async def test_email_schedule_failure_uses_email_specific_help(self) -> None:
        with patch(
            "app.scheduler.plan_schedule_creation",
            AsyncMock(side_effect=SchedulePlanError("模型暂时不可用")),
        ):
            command = await resolve_schedule_command("设置定时任务，分析邮件")

        self.assertEqual(command["schedule_type"], "invalid")
        self.assertIn("邮箱定时分析示例", command["help_text"])
        self.assertNotIn("生产进度", command["help_text"])

    async def test_mode_reply_completes_quoted_creation_request(self) -> None:
        plan = SchedulePlan(
            action="create",
            schedule_type="daily",
            daily_time="08:30",
            prompt="预计日期前三天还没有完成的通知我",
            execution_mode="agent",
        )
        original = "创建定时任务 每天早上8：30执行：预计日期前三天还没有完成的通知我"
        with patch(
            "app.scheduler.plan_schedule_creation",
            AsyncMock(return_value=plan),
        ) as planner:
            command = await resolve_schedule_command(
                "Agent 模式",
                referenced_request_text=original,
            )

        self.assertEqual(command["execution_mode"], "agent")
        self.assertEqual(command["daily_time"], "08:30")
        self.assertEqual(
            planner.await_args.kwargs["original_request"],
            original,
        )


if __name__ == "__main__":
    unittest.main()
