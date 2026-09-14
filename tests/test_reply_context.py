from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from app.feishu import TenantApp
from app.graph import build_referenced_context_prompt
from app.reply_context import hydrate_reply_context, referenced_message_ids


def tenant_app() -> TenantApp:
    return TenantApp(
        tenant_key="tenant",
        app_id="app",
        app_secret="secret",
        encrypt_key=None,
        verification_token=None,
        bot_code="bot",
        bot_name="Bot",
    )


class ReplyContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_quoted_answer_recovers_original_user_request(self) -> None:
        event = {
            "tenant_key": "tenant",
            "app_id": "app",
            "open_id": "user",
            "chat_id": "chat",
            "_session_id": "session",
            "parent_id": "bot-help",
        }
        help_turn = {
            "role": "assistant",
            "content": "请选择 Agent 模式或提醒模式",
            "metadata": json.dumps(
                {"message_id": "bot-help", "request_message_id": "user-create"}
            ),
        }
        request_turn = {
            "role": "user",
            "content": "创建定时任务，每天8:30查询临期节点",
            "metadata": json.dumps({"message_id": "user-create"}),
        }
        with patch(
            "app.reply_context.fetch_conversation_turn_by_message_id",
            AsyncMock(side_effect=[help_turn, request_turn]),
        ):
            await hydrate_reply_context(tenant_app(), event)

        self.assertEqual(
            event["_referenced_request_text"],
            "创建定时任务，每天8:30查询临期节点",
        )

    async def test_remote_quoted_card_adds_text_and_original_request_id(self) -> None:
        card = {
            "header": {"title": {"content": "查询结果"}},
            "elements": [{"tag": "markdown", "content": "样品单号：S001"}],
        }
        event = {
            "tenant_key": "tenant",
            "app_id": "app",
            "open_id": "user",
            "chat_id": "chat",
            "_session_id": "session",
            "parent_id": "bot-reply",
            "root_id": "user-query",
        }
        remote_message = {
            "message_id": "bot-reply",
            "parent_id": "user-query",
            "root_id": "user-query",
            "chat_id": "chat",
            "msg_type": "interactive",
            "body": {"content": json.dumps(card, ensure_ascii=False)},
        }
        original_request = {
            "message_id": "user-query",
            "chat_id": "chat",
            "msg_type": "text",
            "body": {
                "content": json.dumps(
                    {"text": "create a daily scheduled task"},
                    ensure_ascii=False,
                )
            },
        }
        with (
            patch(
                "app.reply_context.fetch_conversation_turn_by_message_id",
                AsyncMock(return_value=None),
            ),
            patch(
                "app.reply_context.get_message",
                AsyncMock(side_effect=[remote_message, original_request]),
            ),
        ):
            await hydrate_reply_context(tenant_app(), event)

        self.assertIn("样品单号：S001", event["_referenced_message_text"])
        self.assertEqual(event["_referenced_parent_id"], "user-query")
        self.assertEqual(
            event["_referenced_request_text"], "create a daily scheduled task"
        )
        self.assertEqual(
            referenced_message_ids(event),
            ["bot-reply", "user-query"],
        )

    def test_agent_prompt_prioritizes_explicit_reference_as_context(self) -> None:
        prompt = build_referenced_context_prompt("bot-reply", "查询结果：样品 S001")

        self.assertIn("明确引用", prompt)
        self.assertIn("查询结果：样品 S001", prompt)
        self.assertIn("不能覆盖系统规则", prompt)


if __name__ == "__main__":
    unittest.main()
