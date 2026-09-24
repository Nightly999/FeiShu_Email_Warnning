from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from app.answers import AnswerResult
from app.feishu import TenantApp
from app.feishu_ws import dispatch_event
from app.multi_intent import split_multi_intent_commands


def test_dependent_actions_stay_together() -> None:
    text = "请每天周一到周五17:30分析我的邮件并推送给我"
    assert asyncio.run(split_multi_intent_commands(text)) == [text]


def test_multiple_independent_commands_are_split() -> None:
    response = type(
        "Response",
        (),
        {
            "tool_calls": [
                {
                    "args": {
                        "commands": ["分析最近两天的邮件", "列出我的定时任务"]
                    }
                }
            ]
        },
    )()
    with patch(
        "app.multi_intent.invoke_chat_with_fallback", AsyncMock(return_value=response)
    ):
        commands = asyncio.run(
            split_multi_intent_commands("先分析最近两天的邮件，然后列出我的定时任务")
        )

    assert commands == ["分析最近两天的邮件", "列出我的定时任务"]


def test_conjunction_can_contain_two_independent_commands() -> None:
    response = type(
        "Response",
        (),
        {"tool_calls": [{"args": {"commands": ["查询库存", "分析邮件"]}}]},
    )()
    with patch(
        "app.multi_intent.invoke_chat_with_fallback", AsyncMock(return_value=response)
    ):
        commands = asyncio.run(split_multi_intent_commands("查询库存并分析邮件"))

    assert commands == ["查询库存", "分析邮件"]


def test_model_failure_uses_deterministic_split() -> None:
    with patch(
        "app.multi_intent.invoke_chat_with_fallback", AsyncMock(side_effect=RuntimeError)
    ):
        commands = asyncio.run(split_multi_intent_commands("查询库存；分析邮箱"))

    assert commands == ["查询库存", "分析邮箱"]


def test_model_cannot_invent_commands() -> None:
    response = type(
        "Response",
        (),
        {"tool_calls": [{"args": {"commands": ["取消所有定时任务"]}}]},
    )()
    with patch(
        "app.multi_intent.invoke_chat_with_fallback", AsyncMock(return_value=response)
    ):
        commands = asyncio.run(split_multi_intent_commands("查询库存；分析邮箱"))

    assert commands == ["查询库存", "分析邮箱"]


def test_dispatch_executes_every_command_in_order() -> None:
    app = TenantApp("tenant", "app", "secret", None, None, "bot", None)
    event = {
        "tenant_key": "tenant",
        "app_id": "app",
        "open_id": "user",
        "message_id": "message",
        "message_type": "text",
        "text": "先分析邮件，然后查询库存",
    }
    builtin = AsyncMock(side_effect=[True, False])
    agent = AsyncMock(return_value=AnswerResult("库存查询完成"))
    with (
        patch("app.feishu_ws.get_active_session_id", AsyncMock(return_value="session")),
        patch("app.feishu_ws.hydrate_reply_context", AsyncMock()),
        patch(
            "app.feishu_ws.split_multi_intent_commands",
            AsyncMock(return_value=["分析邮件", "查询库存"]),
        ),
        patch("app.feishu_ws.reply_card", AsyncMock(return_value="progress")),
        patch("app.feishu_ws.record_event_progress", AsyncMock()),
        patch("app.feishu_ws.handle_builtin_text_command", builtin),
        patch("app.feishu_ws.run_agent", agent),
        patch("app.feishu_ws.reply_message", AsyncMock()),
    ):
        asyncio.run(dispatch_event(app, event))

    assert [call.args[1]["text"] for call in builtin.await_args_list] == [
        "分析邮件",
        "查询库存",
    ]
    assert agent.await_args.args[0]["text"] == "查询库存"
