from __future__ import annotations

import unittest

from app.scheduler import (
    PREVIOUS_QUERY_PROMPT,
    auto_task_name,
    execution_mode_for_prompt,
    parse_schedule_command,
    schedule_help_text,
)


class ScheduleCommandTests(unittest.TestCase):
    def test_generic_creation_request_opens_help(self) -> None:
        command = parse_schedule_command("帮我创个定时任务")
        self.assertEqual(command, {"schedule_type": "help"})
        self.assertIn("每天 09:00", schedule_help_text())

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
        self.assertEqual(morning["daily_time"], "09:00")
        self.assertEqual(afternoon["daily_time"], "15:30")

    def test_unrecognized_management_expression_does_not_reach_model(self) -> None:
        command = parse_schedule_command("删除定时任务")
        self.assertEqual(command["schedule_type"], "invalid")
        self.assertIn("任务编号", command["error"])

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


if __name__ == "__main__":
    unittest.main()
