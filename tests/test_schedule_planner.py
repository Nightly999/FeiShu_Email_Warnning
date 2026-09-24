from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app.schedule_planner import extract_schedule_plan_arguments, plan_schedule_creation


class SchedulePlannerToolTests(unittest.IsolatedAsyncioTestCase):
    def test_text_encoded_multi_times_are_decoded(self) -> None:
        response = type(
            "Response",
            (),
            {
                "tool_calls": [],
                "content": """<tool_call>
<function=plan_scheduled_task>
<parameter=action>create</parameter>
<parameter=schedule_type>daily_multi</parameter>
<parameter=daily_times>["10:00", "21:00"]</parameter>
<parameter=prompt>分析未处理邮件</parameter>
<parameter=execution_mode>agent</parameter>
</function>
</tool_call>""",
            },
        )()

        arguments = extract_schedule_plan_arguments(response)

        self.assertEqual(arguments["daily_times"], ["10:00", "21:00"])

    def test_tool_call_multi_times_string_is_decoded(self) -> None:
        response = type(
            "Response",
            (),
            {
                "content": "",
                "tool_calls": [
                    {
                        "name": "plan_scheduled_task",
                        "args": {
                            "action": "create",
                            "schedule_type": "daily_multi",
                            "daily_times": '["10:00", "21:00"]',
                            "prompt": "分析未处理邮件",
                        },
                    }
                ],
            },
        )()

        arguments = extract_schedule_plan_arguments(response)

        self.assertEqual(arguments["daily_times"], ["10:00", "21:00"])

    def test_tool_call_weekday_list_string_is_decoded(self) -> None:
        response = type(
            "Response",
            (),
            {
                "content": "",
                "tool_calls": [
                    {
                        "name": "plan_scheduled_task",
                        "args": {
                            "action": "create",
                            "schedule_type": "weekly_multi",
                            "weekly_days": "[0, 1, 2, 3, 4]",
                            "daily_time": "17:30",
                            "prompt": "分析我的邮件并推送给我",
                        },
                    }
                ],
            },
        )()

        arguments = extract_schedule_plan_arguments(response)

        self.assertEqual(arguments["weekly_days"], [0, 1, 2, 3, 4])

    def test_mimo_text_encoded_tool_call_is_supported(self) -> None:
        response = type(
            "Response",
            (),
            {
                "tool_calls": [],
                "content": """<tool_call>
<function=plan_scheduled_task>
<parameter=action>create</parameter>
<parameter=schedule_type>daily</parameter>
<parameter=run_at>null</parameter>
<parameter=daily_time>08:30</parameter>
<parameter=interval_minutes>None</parameter>
<parameter=prompt>query unfinished items &amp; notify me</parameter>
<parameter=execution_mode>agent</parameter>
<parameter=task_name>unfinished items</parameter>
<parameter=clarification>null</parameter>
</function>
</tool_call>""",
            },
        )()

        arguments = extract_schedule_plan_arguments(response)

        self.assertEqual(arguments["daily_time"], "08:30")
        self.assertEqual(arguments["prompt"], "query unfinished items & notify me")
        self.assertEqual(arguments["execution_mode"], "agent")
        self.assertIsNone(arguments["run_at"])
        self.assertIsNone(arguments["interval_minutes"])

    async def test_model_must_return_structured_schedule_tool_call(self) -> None:
        response = type(
            "Response",
            (),
            {
                "content": "",
                "tool_calls": [
                    {
                        "name": "plan_scheduled_task",
                        "args": {
                            "action": "create",
                            "schedule_type": "daily",
                            "run_at": None,
                            "daily_time": "08:30",
                            "interval_minutes": None,
                            "prompt": "查询预计日期前三天仍未完成的节点并通知我",
                            "execution_mode": "agent",
                            "task_name": "临期未完成通知",
                            "clarification": None,
                        },
                    }
                ],
            },
        )()
        with patch(
            "app.schedule_planner.invoke_chat_with_fallback",
            AsyncMock(return_value=response),
        ) as model:
            plan = await plan_schedule_creation(
                "创建定时任务，每天早上8:30查询临期未完成节点并通知我"
            )

        self.assertEqual(plan.schedule_type, "daily")
        self.assertEqual(plan.daily_time, "08:30")
        self.assertEqual(plan.execution_mode, "agent")
        tools = model.await_args.kwargs["tools"]
        self.assertEqual(tools[0]["function"]["name"], "plan_scheduled_task")


if __name__ == "__main__":
    unittest.main()
