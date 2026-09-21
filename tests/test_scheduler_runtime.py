from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.answers import AnswerResult
from app.bootstrap import bootstrap
from app.db import execute, fetch_one
from app.email_service import EmailCommandResult
from app.feishu import TenantApp
from app.scheduler import (
    PREVIOUS_QUERY_PROMPT,
    cancel_all_scheduled_tasks,
    create_scheduled_task,
    list_scheduled_tasks,
    manage_scheduled_task,
    scheduled_task_history,
)
from app.scheduler_runtime import next_weekly_run, run_due_tasks
from app.memory.repository import add_conversation_turn
from app.settings import get_settings


def tenant_app() -> TenantApp:
    return TenantApp(
        tenant_key="tenant",
        app_id="app",
        app_secret="secret",
        encrypt_key="encrypt-key",
        verification_token="verification-token",
        bot_code="bot",
        bot_name="Bot",
    )


class SchedulerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_next_weekly_run_respects_same_day_and_week_boundary(self) -> None:
        with patch("app.scheduler_runtime.now_datetime", return_value=datetime(2026, 9, 18, 17, 29)):
            self.assertEqual(next_weekly_run(4, "17:30"), "2026-09-18 17:30:00")
        with patch("app.scheduler_runtime.now_datetime", return_value=datetime(2026, 9, 18, 17, 30)):
            self.assertEqual(next_weekly_run(4, "17:30"), "2026-09-25 17:30:00")
        with patch("app.scheduler_runtime.now_datetime", return_value=datetime(2026, 9, 20, 10, 0)):
            self.assertEqual(next_weekly_run(0, "09:00"), "2026-09-21 09:00:00")

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self.temp_dir.name)
        self.previous = {
            key: os.environ.get(key)
            for key in ("APP_DATABASE_PATH", "FEISHU_APPS_CONFIG_PATH", "APP_ENV")
        }
        os.environ["APP_DATABASE_PATH"] = str(self.root / "agent.db")
        os.environ["FEISHU_APPS_CONFIG_PATH"] = str(self.root / "missing-apps.json")
        os.environ["APP_ENV"] = "production"
        get_settings.cache_clear()
        await bootstrap()

    async def asyncTearDown(self) -> None:
        get_settings.cache_clear()
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp_dir.cleanup()

    async def insert_due_task(
        self,
        *,
        execution_mode: str,
        schedule_type: str = "once",
        prompt: str = "查询样品风险并汇总",
        interval_minutes: int | None = None,
    ) -> int:
        await execute(
            """
            INSERT INTO scheduled_task (
              tenant_key, app_id, bot_code, chat_id, chat_type, open_id,
              schedule_type, run_at, daily_time, interval_minutes, prompt, next_run_at,
              execution_mode, timeout_seconds, max_retries, timezone, last_status
            ) VALUES (
              'tenant', 'app', 'bot', 'chat', 'p2p', 'user',
              ?, '2000-01-01 00:00:00', ?, ?, ?, '2000-01-01 00:00:00',
              ?, 30, 5, 'Asia/Shanghai', 'idle'
            )
            """,
            (
                schedule_type,
                "09:00" if schedule_type == "daily" else None,
                interval_minutes,
                prompt,
                execution_mode,
            ),
        )
        row = await fetch_one("SELECT MAX(id) AS id FROM scheduled_task")
        return int(row["id"])

    async def test_create_and_manage_agent_task(self) -> None:
        event = {
            "tenant_key": "tenant",
            "app_id": "app",
            "open_id": "user",
            "chat_id": "chat",
            "chat_type": "p2p",
        }
        answer = await create_scheduled_task(
            tenant_app(),
            event,
            {
                "schedule_type": "daily",
                "daily_time": "09:00",
                "prompt": "查询样品风险并汇总",
            },
        )
        task = await fetch_one("SELECT * FROM scheduled_task")
        self.assertIn(f"#{task['id']}", answer)
        self.assertEqual(task["task_name"], "样品风险汇总")
        self.assertIn("任务名称：样品风险汇总", answer)
        self.assertIn("执行时间：每天 09:00", answer)
        self.assertEqual(task["execution_mode"], "agent")
        self.assertEqual(task["chat_type"], "p2p")

        await manage_scheduled_task(
            tenant_app(), event, int(task["id"]), "pause"
        )
        paused = await fetch_one(
            "SELECT enabled, last_status FROM scheduled_task WHERE id = ?",
            (task["id"],),
        )
        self.assertEqual(paused, {"enabled": 0, "last_status": "paused"})

        await manage_scheduled_task(
            tenant_app(), event, int(task["id"]), "run_now"
        )
        queued = await fetch_one(
            "SELECT enabled, last_status FROM scheduled_task WHERE id = ?",
            (task["id"],),
        )
        self.assertEqual(queued, {"enabled": 1, "last_status": "queued"})

    async def test_weekly_email_task_is_created_and_repeats(self) -> None:
        event = {
            "tenant_key": "tenant", "app_id": "app", "open_id": "user",
            "chat_id": "chat", "chat_type": "p2p",
        }
        answer = await create_scheduled_task(
            tenant_app(), event,
            {"schedule_type": "weekly", "weekly_day": "4", "daily_time": "17:30", "prompt": "分析我的邮件"},
        )
        task = await fetch_one("SELECT * FROM scheduled_task")
        self.assertIn("每周五 17:30", answer)
        self.assertEqual(task["weekly_day"], 4)
        self.assertEqual(task["execution_mode"], "agent")
        self.assertEqual(task["next_run_at"], next_weekly_run(4, "17:30"))
        self.assertIn("每周五 17:30", await list_scheduled_tasks(tenant_app(), event))

        await execute("UPDATE scheduled_task SET next_run_at = '2000-01-01 00:00:00' WHERE id = ?", (task["id"],))
        with (
            patch("app.email_service.handle_email_command", AsyncMock(return_value=EmailCommandResult("完成"))),
            patch("app.scheduler_runtime.send_card", AsyncMock(return_value="message-id")),
        ):
            await run_due_tasks({"app": tenant_app()})
        task = await fetch_one("SELECT * FROM scheduled_task WHERE id = ?", (task["id"],))
        self.assertEqual(task["enabled"], 1)
        self.assertEqual(task["next_run_at"], next_weekly_run(4, "17:30"))

    async def test_cancel_all_only_affects_current_user_scope(self) -> None:
        own_first = await self.insert_due_task(execution_mode="agent")
        own_second = await self.insert_due_task(execution_mode="agent")
        await execute(
            "UPDATE scheduled_task SET open_id = 'other-user' WHERE id = ?",
            (own_second,),
        )
        event = {
            "tenant_key": "tenant",
            "app_id": "app",
            "open_id": "user",
            "chat_id": "chat",
            "chat_type": "p2p",
        }

        answer = await cancel_all_scheduled_tasks(tenant_app(), event)

        own = await fetch_one(
            "SELECT enabled, last_status FROM scheduled_task WHERE id = ?",
            (own_first,),
        )
        other = await fetch_one(
            "SELECT enabled, last_status FROM scheduled_task WHERE id = ?",
            (own_second,),
        )
        self.assertEqual(answer, "已取消当前用户的 1 个定时任务。")
        self.assertEqual(own, {"enabled": 0, "last_status": "cancelled"})
        self.assertEqual(other, {"enabled": 1, "last_status": "idle"})

    async def test_generated_names_are_unique_and_explicit_duplicates_are_rejected(self) -> None:
        event = {
            "tenant_key": "tenant",
            "app_id": "app",
            "open_id": "user",
            "chat_id": "chat",
            "chat_type": "p2p",
        }
        command = {
            "schedule_type": "daily",
            "daily_time": "09:00",
            "prompt": "查询样品风险并汇总",
        }
        await create_scheduled_task(tenant_app(), event, command)
        await create_scheduled_task(tenant_app(), event, command)
        rows = await fetch_one(
            """
            SELECT GROUP_CONCAT(task_name, '|') AS names
            FROM scheduled_task ORDER BY id
            """
        )
        self.assertEqual(rows["names"], "样品风险汇总|样品风险汇总（2）")

        explicit = dict(command, task_name="样品日报")
        await create_scheduled_task(tenant_app(), event, explicit)
        duplicate_answer = await create_scheduled_task(
            tenant_app(), event, explicit
        )
        self.assertIn("已经存在", duplicate_answer)

    async def test_interval_agent_inherits_previous_business_query(self) -> None:
        event = {
            "tenant_key": "tenant",
            "app_id": "app",
            "open_id": "user",
            "chat_id": "chat",
            "chat_type": "p2p",
            "_session_id": "session-current",
        }
        await add_conversation_turn(
            tenant_key="tenant",
            app_id="app",
            open_id="user",
            chat_id="chat",
            session_id="session-current",
            role="user",
            content="2026年5月1日至今样品的风险，帮我汇总",
        )
        answer = await create_scheduled_task(
            tenant_app(),
            event,
            {
                "schedule_type": "interval",
                "interval_minutes": "5",
                "prompt": PREVIOUS_QUERY_PROMPT,
                "execution_mode": "agent",
            },
        )
        task = await fetch_one("SELECT * FROM scheduled_task")
        self.assertEqual(task["prompt"], "2026年5月1日至今样品的风险，帮我汇总")
        self.assertEqual(task["execution_mode"], "agent")
        self.assertEqual(task["interval_minutes"], 5)
        self.assertIn("执行时间：每 5 分钟", answer)

    async def test_task_list_and_run_history_are_paginated(self) -> None:
        event = {
            "tenant_key": "tenant",
            "app_id": "app",
            "open_id": "user",
            "chat_id": "chat",
            "chat_type": "p2p",
        }
        for number in range(1, 7):
            await create_scheduled_task(
                tenant_app(),
                event,
                {
                    "schedule_type": "daily",
                    "daily_time": "09:00",
                    "prompt": f"提醒我处理事项{number}",
                },
            )

        first_page = await list_scheduled_tasks(tenant_app(), event, page=1)
        second_page = await list_scheduled_tasks(tenant_app(), event, page=2)
        self.assertIn("第 1/2 页，共 6 个", first_page)
        self.assertIn("下一页：查看定时任务 第2页", first_page)
        self.assertIn("第 2/2 页，共 6 个", second_page)
        self.assertIn("上一页：查看定时任务 第1页", second_page)

        task = await fetch_one("SELECT id FROM scheduled_task ORDER BY id LIMIT 1")
        for number in range(1, 7):
            await execute(
                """
                INSERT INTO scheduled_task_run (
                  run_id, task_id, status, attempt, started_at, finished_at
                ) VALUES (?, ?, 'success', 1, ?, ?)
                """,
                (
                    f"run-{number}",
                    task["id"],
                    f"2026-08-16 09:0{number}:00",
                    f"2026-08-16 09:0{number}:01",
                ),
            )
        history = await scheduled_task_history(
            tenant_app(), event, int(task["id"]), page=2
        )
        self.assertIn("第 2/2 页，共 6 条", history)
        self.assertIn("上一页：查看定时任务", history)

    async def test_agent_task_runs_in_isolated_automation_session(self) -> None:
        task_id = await self.insert_due_task(execution_mode="agent")
        agent = AsyncMock(
            return_value=AnswerResult(
                "| 样品单号 | 风险等级 |\n|---|---|\n| A001 | 高风险 |",
                "success",
            )
        )
        delivery = AsyncMock(return_value="message-id")

        with (
            patch("app.scheduler_runtime.run_agent", agent),
            patch("app.scheduler_runtime.send_card", delivery),
        ):
            await run_due_tasks({"app": tenant_app()})

        event = agent.await_args.args[0]
        self.assertTrue(event["_automation_run"])
        self.assertTrue(event["_session_id"].startswith(f"automation:{task_id}:"))
        self.assertEqual(event["chat_type"], "p2p")
        delivered = delivery.await_args.args[2]
        self.assertEqual(
            delivered["header"]["title"]["content"], "定时任务执行结果"
        )
        self.assertTrue(
            any(
                element.get("tag") == "table"
                for element in delivered["body"]["elements"]
            )
        )

        task = await fetch_one("SELECT * FROM scheduled_task WHERE id = ?", (task_id,))
        run = await fetch_one(
            "SELECT * FROM scheduled_task_run WHERE task_id = ?", (task_id,)
        )
        self.assertEqual(task["enabled"], 0)
        self.assertEqual(task["last_status"], "success")
        self.assertEqual(run["status"], "success")

    async def test_reminder_task_does_not_start_agent(self) -> None:
        await self.insert_due_task(
            execution_mode="reminder", prompt="提醒我提交日报"
        )
        agent = AsyncMock()
        delivery = AsyncMock(return_value="message-id")

        with (
            patch("app.scheduler_runtime.run_agent", agent),
            patch("app.scheduler_runtime.send_message", delivery),
        ):
            await run_due_tasks({"app": tenant_app()})

        agent.assert_not_awaited()
        self.assertEqual(
            delivery.await_args.kwargs["content"]["text"],
            "定时提醒：提醒我提交日报",
        )

    async def test_scheduled_email_delivers_email_briefing_card(self) -> None:
        await self.insert_due_task(
            execution_mode="agent",
            prompt="分析最近三天的前五封未处理邮件",
        )
        email_card = {
            "schema": "2.0",
            "header": {"title": {"content": "📬 AI 邮件智能简报"}},
            "body": {"elements": []},
        }
        delivery = AsyncMock(return_value="message-id")

        with (
            patch(
                "app.email_service.handle_email_command",
                AsyncMock(return_value=EmailCommandResult("邮件分析", card=email_card)),
            ),
            patch("app.scheduler_runtime.send_card", delivery),
        ):
            await run_due_tasks({"app": tenant_app()})

        self.assertIs(delivery.await_args.args[2], email_card)

    async def test_interval_agent_remains_enabled_after_success(self) -> None:
        task_id = await self.insert_due_task(
            execution_mode="agent",
            schedule_type="interval",
            interval_minutes=5,
        )
        agent = AsyncMock(return_value=AnswerResult("风险样品共 3 条", "success"))
        with (
            patch("app.scheduler_runtime.run_agent", agent),
            patch("app.scheduler_runtime.send_card", AsyncMock(return_value="message-id")),
        ):
            await run_due_tasks({"app": tenant_app()})

        task = await fetch_one("SELECT * FROM scheduled_task WHERE id = ?", (task_id,))
        self.assertEqual(task["enabled"], 1)
        self.assertEqual(task["last_status"], "success")
        self.assertGreater(task["next_run_at"], "2000-01-01 00:00:00")

    async def test_transient_agent_error_is_retried(self) -> None:
        task_id = await self.insert_due_task(execution_mode="agent")
        agent = AsyncMock(return_value=AnswerResult("模型暂时不可用", "error"))

        with (
            patch("app.scheduler_runtime.run_agent", agent),
            patch("app.scheduler_runtime.send_message", AsyncMock()),
        ):
            await run_due_tasks({"app": tenant_app()})

        task = await fetch_one("SELECT * FROM scheduled_task WHERE id = ?", (task_id,))
        run = await fetch_one(
            "SELECT * FROM scheduled_task_run WHERE task_id = ?", (task_id,)
        )
        self.assertEqual(task["enabled"], 1)
        self.assertEqual(task["last_status"], "retrying")
        self.assertEqual(task["consecutive_failures"], 1)
        self.assertEqual(run["status"], "error")

    async def test_permission_denial_disables_task_and_notifies_user(self) -> None:
        task_id = await self.insert_due_task(execution_mode="agent")
        agent = AsyncMock(return_value=AnswerResult("当前用户无权查询", "denied"))
        delivery = AsyncMock(return_value="message-id")

        with (
            patch("app.scheduler_runtime.run_agent", agent),
            patch("app.scheduler_runtime.send_message", delivery),
        ):
            await run_due_tasks({"app": tenant_app()})

        task = await fetch_one("SELECT * FROM scheduled_task WHERE id = ?", (task_id,))
        self.assertEqual(task["enabled"], 0)
        self.assertEqual(task["last_status"], "error")
        self.assertIn("已停止", delivery.await_args.kwargs["content"]["text"])


if __name__ == "__main__":
    unittest.main()
