from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app.builtin_commands import deliver_builtin_answer
from app.feishu import TenantApp
from app.memory.commands import MemoryCommand, parse_memory_command


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


class MemoryCommandIntentTests(unittest.TestCase):
    def test_natural_session_and_memory_commands_are_parsed(self) -> None:
        self.assertEqual(parse_memory_command("新建会话"), MemoryCommand("new"))
        self.assertEqual(parse_memory_command("/clear"), MemoryCommand("new"))
        self.assertEqual(parse_memory_command("查看我的记忆"), MemoryCommand("list"))
        self.assertEqual(
            parse_memory_command("请记住我的语言是中文"),
            MemoryCommand("remember", "我的语言是中文"),
        )
        self.assertEqual(
            parse_memory_command("忘掉我的语言"),
            MemoryCommand("forget", "我的语言"),
        )


class BuiltinDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_error_title_is_used(self) -> None:
        with patch(
            "app.builtin_commands.reply_card",
            AsyncMock(return_value="message-new"),
        ) as reply:
            await deliver_builtin_answer(
                tenant_app(),
                message_id="message-original",
                progress_message_id=None,
                question="设置邮箱定时分析",
                answer="请补充执行时间",
                status="error",
                title="邮箱定时设置失败",
            )

        card = reply.await_args.args[2]
        self.assertEqual(card["header"]["template"], "red")
        self.assertEqual(card["header"]["title"]["content"], "邮箱定时设置失败")

    async def test_failed_progress_update_replies_with_new_card(self) -> None:
        with (
            patch(
                "app.builtin_commands.update_card",
                AsyncMock(return_value=False),
            ) as update,
            patch(
                "app.builtin_commands.reply_card",
                AsyncMock(return_value="message-new"),
            ) as reply,
        ):
            await deliver_builtin_answer(
                tenant_app(),
                message_id="message-original",
                progress_message_id="message-progress",
                question="查看我的定时任务",
                answer="当前没有定时任务",
            )

        update.assert_awaited_once()
        reply.assert_awaited_once()
        self.assertEqual(reply.await_args.args[1], "message-original")
