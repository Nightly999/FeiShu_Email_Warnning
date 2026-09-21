from __future__ import annotations

import asyncio
import base64
import os
import poplib
from unittest.mock import AsyncMock

import pytest
from lark_oapi.ws.const import HEADER_SEQ, HEADER_SUM, HEADER_TYPE
from lark_oapi.ws.pb.pbbp2_pb2 import Frame

from app.email_pop3 import EmailAuthenticationError, count_pop3_messages, parse_message
from app.email_security import decrypt_email_password, encrypt_email_password
from app.email_service import (
    EmailAnalysis,
    EmailPlan,
    _fallback_plan,
    _likely_needs_reply,
    _select_messages,
    _validated_plan,
    analyze_pending,
    build_email_login_card,
    build_email_report,
    build_email_report_card,
    plan_email_request,
    sync_analyze_report,
    handle_email_command,
)
from app.feishu import TenantApp
from app.feishu_ws import (
    CardCallbackWsClient,
    extract_email_bind_card_action,
    process_email_bind_card,
)
from app.logging_security import redact_sensitive_text
from app.settings import get_settings


def test_pop3_accepts_long_html_lines() -> None:
    assert poplib._MAXLINE == 10 * 1024 * 1024


def test_pop3_mailbox_count_uses_stat_and_closes_connection(monkeypatch) -> None:
    class Client:
        closed = False

        def stat(self):
            return 237, 1024

        def quit(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr("app.email_pop3._login", lambda *args: client)
    assert count_pop3_messages(host="pop.example.com", port=995, username="user", password="secret", timeout=10) == 237
    assert client.closed


def test_email_password_is_authenticated_encrypted() -> None:
    previous = os.environ.get("EMAIL_CREDENTIAL_KEY")
    os.environ["EMAIL_CREDENTIAL_KEY"] = base64.urlsafe_b64encode(b"k" * 32).decode()
    get_settings.cache_clear()
    try:
        ciphertext = encrypt_email_password("real-password")
        assert "real-password" not in ciphertext
        assert decrypt_email_password(ciphertext) == "real-password"
        damaged = ciphertext[:-2] + ("AA" if ciphertext[-2:] != "AA" else "BB")
        try:
            decrypt_email_password(damaged)
        except Exception:
            pass
        else:
            raise AssertionError("tampered credential must not decrypt")
    finally:
        if previous is None:
            os.environ.pop("EMAIL_CREDENTIAL_KEY", None)
        else:
            os.environ["EMAIL_CREDENTIAL_KEY"] = previous
        get_settings.cache_clear()


def test_parse_email_keeps_body_and_attachment_metadata_only() -> None:
    raw = b"""From: Sender <sender@example.com>\r
To: User <user@example.com>\r
Cc: Other <other@example.com>\r
Subject: =?utf-8?b?5rWL6K+V6YKu5Lu2?=\r
Date: Tue, 15 Sep 2026 08:00:00 +0800\r
Message-ID: <mail-1@example.com>\r
MIME-Version: 1.0\r
Content-Type: multipart/mixed; boundary=x\r
\r
--x\r
Content-Type: text/plain; charset=utf-8\r
\r
Please review before Friday.\r
--x\r
Content-Type: application/pdf\r
Content-Disposition: attachment; filename=report.pdf\r
Content-Transfer-Encoding: base64\r
\r
UERGREFUQQ==\r
--x--\r
"""

    parsed = parse_message("uid-1", raw, 50_000)

    assert parsed.subject == "测试邮件"
    assert parsed.to[0]["address"] == "user@example.com"
    assert parsed.cc[0]["address"] == "other@example.com"
    assert "Please review" in parsed.text_body
    assert parsed.attachments == [
        {"name": "report.pdf", "mime_type": "application/pdf", "size": 7}
    ]


def test_email_fallback_plan_applies_scope_and_bounds() -> None:
    settings = get_settings()
    plan = _fallback_plan("查看最近3天抄送给我的重要邮件", settings)
    assert plan.lookback_hours == 72
    assert plan.scope == "cc"
    assert plan.important_only is True

    retention = _fallback_plan("邮件分析保留999天", settings)
    assert retention.action == "retention"
    assert retention.retention_days == settings.email_max_retention_days
    assert _fallback_plan("重新登录邮箱", settings).action == "rebind"
    assert _fallback_plan("登录邮箱", settings).action == "rebind"
    assert _fallback_plan("邮箱登录", settings).action == "rebind"

    hourly = _fallback_plan("分析最近12小时的前5封未处理邮件", settings)
    assert hourly.lookback_hours == 12
    assert hourly.limit == 5
    assert hourly.only_unprocessed is True

    reply_needed = _fallback_plan("告诉我哪些邮件我没有回复", settings)
    assert reply_needed.reply_needed_only is True

    history = _fallback_plan("帮我分析历史邮件前80份", settings)
    assert history.lookback_hours == 24 * 365 * 10
    assert history.limit == 80
    assert _fallback_plan("有多少封重要邮件", settings).action == "query"


def test_explicit_email_login_skips_model_planning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = AsyncMock()
    monkeypatch.setattr("app.email_service.invoke_chat_with_fallback", model)

    plan = asyncio.run(plan_email_request("登录邮箱"))

    assert plan.action == "rebind"
    model.assert_not_awaited()


@pytest.mark.parametrize(
    ("question", "kind", "hours"),
    [
        ("邮箱里一共有多少封邮件", "all", 48),
        ("邮箱里有多少邮件", "all", 48),
        ("最近48小时有多少封邮件", "recent", 48),
        ("这里一共有多少封未读邮件", "unread", 48),
    ],
)
def test_count_questions_skip_model_planning(monkeypatch, question, kind, hours) -> None:
    model = AsyncMock()
    monkeypatch.setattr("app.email_service.invoke_chat_with_fallback", model)
    plan = asyncio.run(plan_email_request(question))
    assert plan.action == "count"
    assert plan.count_kind == kind
    assert plan.lookback_hours == hours
    model.assert_not_awaited()


def test_count_commands_do_not_analyze_or_truncate(monkeypatch) -> None:
    account = {
        "id": 12,
        "email_address": "user@example.com",
        "pop3_host": "pop.example.com",
        "pop3_port": 995,
        "password_ciphertext": "encrypted",
    }
    monkeypatch.setattr("app.email_service.get_email_account", AsyncMock(return_value=account))
    monkeypatch.setattr("app.email_service.decrypt_email_password", lambda _: "password")

    def mailbox_count(**_) -> int:
        return 237

    monkeypatch.setattr("app.email_service.count_pop3_messages", mailbox_count)
    recent_count = AsyncMock(return_value=153)
    monkeypatch.setattr("app.email_service.count_recent_messages", recent_count)
    sync = AsyncMock(return_value=0)
    monkeypatch.setattr("app.email_service.sync_account", sync)
    analyze = AsyncMock()
    monkeypatch.setattr("app.email_service.analyze_pending", analyze)
    event = {"tenant_key": "tenant", "app_id": "app", "open_id": "user"}

    total = asyncio.run(handle_email_command({**event, "text": "邮箱里一共有多少封邮件"}))
    assert "237" in total.answer and "收件箱" in total.answer
    assert total.card is None
    sync.assert_not_awaited()

    recent = asyncio.run(handle_email_command({**event, "text": "最近48小时有多少封邮件"}))
    assert "153" in recent.answer and "已同步" in recent.answer
    sync.assert_awaited_once()
    recent_count.assert_awaited_once_with(12, 48)
    analyze.assert_not_awaited()

    unread = asyncio.run(handle_email_command({**event, "text": "有多少封未读邮件"}))
    assert "无法" in unread.answer and "未读" in unread.answer and "POP3" in unread.answer
    assert "153" not in unread.answer
    analyze.assert_not_awaited()


def test_model_recognizes_dynamic_email_account_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = type(
        "Response",
        (),
        {"tool_calls": [{"name": "plan_email_request", "args": {"action": "rebind"}}]},
    )()
    monkeypatch.setattr(
        "app.email_service.invoke_chat_with_fallback",
        AsyncMock(return_value=response),
    )

    plan = asyncio.run(plan_email_request("更换账号"))

    assert plan.action == "rebind"


def test_unrelated_command_returns_to_general_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = type(
        "Response",
        (),
        {"tool_calls": [{"name": "plan_email_request", "args": {"action": "unrelated"}}]},
    )()
    account_lookup = AsyncMock()
    monkeypatch.setattr(
        "app.email_service.invoke_chat_with_fallback",
        AsyncMock(return_value=response),
    )
    monkeypatch.setattr("app.email_service.get_email_account", account_lookup)

    result = asyncio.run(
        handle_email_command(
            {
                "text": "查询生产进度",
                "tenant_key": "tenant",
                "app_id": "app",
                "open_id": "user",
            }
        )
    )

    assert result is None
    account_lookup.assert_not_awaited()


def test_explicit_numbers_override_inconsistent_model_plan() -> None:
    plan = _validated_plan(
        EmailPlan(action="query", lookback_hours=48, limit=3, only_unprocessed=False),
        get_settings(),
        "分析最近7天的前5封未处理邮件",
    )

    assert plan.lookback_hours == 168
    assert plan.limit == 5
    assert plan.only_unprocessed is True


def test_email_plan_selects_requested_messages_before_analysis() -> None:
    messages = [
        {
            "id": 1,
            "subject": "报价确认",
            "sender_name": "张三",
            "sender_address": "zhang@example.com",
            "text_body": "项目甲",
            "push_status": "pending",
        },
        {
            "id": 2,
            "subject": "普通通知",
            "sender_name": "李四",
            "sender_address": "li@example.com",
            "text_body": "项目乙",
            "push_status": "success",
        },
        {
            "id": 3,
            "subject": "报价更新",
            "sender_name": "张三",
            "sender_address": "zhang@example.com",
            "text_body": "项目甲",
            "push_status": "pending",
        },
    ]
    plan = EmailPlan(
        action="query",
        sender_contains="张三",
        keywords=["项目甲"],
        message_positions=[2],
        only_unprocessed=True,
        limit=1,
    )

    assert [item["id"] for item in _select_messages(messages, plan)] == [3]


def test_analysis_failure_is_not_reported_as_no_matching_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = {
        "id": 1,
        "to_json": '[{"address":"user@example.com"}]',
        "cc_json": "[]",
        "text_body": "",
        "html_body": "",
        "analysis_status": "pending",
        "push_status": "pending",
        "summary": None,
    }
    failed = dict(message, analysis_status="failed")
    monkeypatch.setattr("app.email_service.sync_account", AsyncMock(return_value=0))
    monkeypatch.setattr(
        "app.email_service.list_recent_messages",
        AsyncMock(side_effect=[[message], [failed]]),
    )
    monkeypatch.setattr("app.email_service._display_name", AsyncMock(return_value=""))
    monkeypatch.setattr("app.email_service.analyze_pending", AsyncMock())

    result = asyncio.run(
        sync_analyze_report(
            {"message_id": "message-1"},
            {"id": 1, "email_address": "user@example.com", "retention_days": 7},
            EmailPlan(action="query"),
        )
    )

    assert result.status == "error"
    assert "大模型分析暂时不可用" in result.answer


def test_scheduled_report_keeps_previously_pushed_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = {
        "id": 314,
        "subject": "Microsoft Outlook 测试消息",
        "sender_name": "Microsoft Outlook",
        "sender_address": "support@example.com",
        "to_json": '[{"address":"user@example.com"}]',
        "cc_json": "[]",
        "text_body": "测试邮件",
        "html_body": "",
        "analysis_status": "success",
        "summary": "账户设置测试邮件。",
        "importance": "低",
        "requires_attention": False,
        "relation_type": "To",
        "todos_json": "[]",
        "risks_json": "[]",
        "attachments_json": "[]",
    }
    monkeypatch.setattr("app.email_service.sync_account", AsyncMock(return_value=0))
    monkeypatch.setattr(
        "app.email_service.list_recent_messages",
        AsyncMock(side_effect=[[message], [message]]),
    )
    monkeypatch.setattr("app.email_service._display_name", AsyncMock(return_value=""))
    monkeypatch.setattr("app.email_service.analyze_pending", AsyncMock())
    monkeypatch.setattr("app.email_service.create_push_logs", AsyncMock())

    result = asyncio.run(
        sync_analyze_report(
            {"message_id": "scheduled:6:run", "_automation_run": True, "_email_task_id": 6},
            {"id": 1, "email_address": "user@example.com", "retention_days": 7},
            EmailPlan(action="query", lookback_hours=48),
        )
    )

    assert result.status == "success"
    assert "Microsoft Outlook 测试消息" in result.answer


def test_invalid_analysis_batch_retries_each_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = [{"id": 1, "summary": None}, {"id": 2, "summary": None}]
    analyses = [
        EmailAnalysis(
            message_id=message_id,
            summary=f"摘要 {message_id}",
            importance="中",
            requires_attention=True,
            relation_type="To",
        )
        for message_id in (1, 2)
    ]
    analyze = AsyncMock(side_effect=[ValueError("invalid batch"), [analyses[0]], [analyses[1]]])
    save = AsyncMock()
    failed = AsyncMock()
    monkeypatch.setattr("app.email_service._analyze_batch", analyze)
    monkeypatch.setattr("app.email_service.save_analysis", save)
    monkeypatch.setattr("app.email_service.mark_analysis_failed", failed)

    asyncio.run(analyze_pending(messages, 7, "user@example.com", "User"))

    assert analyze.await_count == 3
    assert save.await_count == 2
    failed.assert_not_awaited()


def test_partial_analysis_failure_is_visible_to_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = {
        "to_json": '[{"address":"user@example.com"}]',
        "cc_json": "[]",
        "text_body": "",
        "html_body": "",
        "push_status": "pending",
    }
    pending = [
        dict(base, id=1, analysis_status="pending", summary=None),
        dict(base, id=2, analysis_status="pending", summary=None),
    ]
    refreshed = [
        dict(
            base,
            id=1,
            analysis_status="success",
            summary="需要回复客户。",
            importance="高",
            requires_attention=True,
            relation_type="To",
            todos_json="[]",
            risks_json="[]",
            attachments_json="[]",
        ),
        dict(base, id=2, analysis_status="failed", summary=None),
    ]
    monkeypatch.setattr("app.email_service.sync_account", AsyncMock(return_value=0))
    monkeypatch.setattr(
        "app.email_service.list_recent_messages",
        AsyncMock(side_effect=[pending, refreshed]),
    )
    monkeypatch.setattr("app.email_service._display_name", AsyncMock(return_value=""))
    monkeypatch.setattr("app.email_service.analyze_pending", AsyncMock())
    monkeypatch.setattr("app.email_service.create_push_logs", AsyncMock())

    result = asyncio.run(
        sync_analyze_report(
            {"message_id": "message-1"},
            {"id": 1, "email_address": "user@example.com", "retention_days": 7},
            EmailPlan(action="query"),
        )
    )

    assert result.status == "success"
    assert "1 封邮件本次分析失败" in result.answer
    assert any(
        "1 封邮件本次分析失败" in item.get("content", "")
        for item in result.card["body"]["elements"]
    )


def test_email_report_has_no_action_buttons() -> None:
    report = build_email_report(
        {"email_address": "user@example.com"},
        [
            {
                "subject": "报价确认",
                "sender_name": "供应商",
                "importance": "高",
                "relation_type": "To",
                "summary": "需要确认报价。",
                "todos_json": '["确认报价"]',
                "possible_owner": "采购部",
                "deadline": "本周五",
                "risks_json": '["逾期风险"]',
                "attachments_json": '[{"name":"quote.pdf"}]',
            }
        ],
        _fallback_plan("分析最近两天的重要邮件", get_settings()),
    )
    assert "确认报价" in report
    assert "quote.pdf" in report
    assert "已处理" not in report
    assert "忽略按钮" not in report


def test_email_report_card_is_an_ai_briefing_without_table() -> None:
    card = build_email_report_card(
        {"email_address": "user@example.com"},
        [
            {
                "subject": "报价确认",
                "sender_name": "供应商",
                "importance": "高",
                "requires_attention": True,
                "relation_type": "To",
                "summary": "供应商等待报价确认，建议今天回复。",
                "todos_json": '["确认报价并回复"]',
                "possible_owner": "采购部",
                "deadline": "今天",
                "risks_json": '["逾期可能影响交付"]',
                "attachments_json": '[{"name":"quote.pdf"}]',
            }
        ],
        EmailPlan(action="query"),
    )

    assert card["header"]["title"]["content"] == "邮件分析简报"
    assert all(item["tag"] != "table" for item in card["body"]["elements"])
    content = "\n".join(
        item.get("content", "") for item in card["body"]["elements"]
    )
    assert "分析结果" in content
    assert "确认报价并回复" in content
    assert "逾期可能影响交付" in content


def test_reply_needed_report_is_clearly_a_pop3_suggestion() -> None:
    plan = EmailPlan(action="query", reply_needed_only=True)
    card = build_email_report_card(
        {"email_address": "user@example.com"},
        [],
        plan,
    )

    assert card["header"]["title"]["content"] == "待回复邮件建议"
    content = "\n".join(
        item.get("content", "") for item in card["body"]["elements"]
    )
    assert "疑似需要你回复" in content
    assert "POP3 无法读取已发送邮件" in content


def test_reply_needed_filter_requires_an_action_and_excludes_no_reply() -> None:
    assert _likely_needs_reply(
        {
            "requires_attention": True,
            "sender_address": "supplier@example.com",
            "todos_json": '["确认报价并回复"]',
        }
    )
    assert not _likely_needs_reply(
        {
            "requires_attention": True,
            "sender_address": "no-reply@example.com",
            "todos_json": '["查看通知"]',
        }
    )
    assert not _likely_needs_reply(
        {
            "requires_attention": True,
            "sender_address": "supplier@example.com",
            "todos_json": "[]",
        }
    )


def test_history_report_card_states_when_details_are_truncated() -> None:
    messages = [
        {
            "subject": f"历史邮件 {index}",
            "sender_address": "sender@example.com",
            "importance": "中",
            "requires_attention": False,
            "relation_type": "To",
            "summary": "测试摘要",
            "todos_json": "[]",
            "risks_json": "[]",
            "attachments_json": "[]",
        }
        for index in range(1, 81)
    ]

    card = build_email_report_card(
        {"email_address": "user@example.com"},
        messages,
        EmailPlan(action="query", limit=80),
    )
    content = "\n".join(item.get("content", "") for item in card["body"]["elements"])

    assert "共 **80** 封" in content
    assert "历史邮件" in content
    assert "87600" not in content
    assert "已完成 **80** 封邮件分析" in content
    assert "最新 **20** 封" in content


def test_bind_card_uses_password_input_and_show_toggle() -> None:
    card = build_email_login_card("绑定公司邮箱", "one-time-token")
    form = card["body"]["elements"][1]
    password = form["elements"][1]
    submit = form["elements"][2]

    assert card["config"]["enable_forward"] is False
    assert password["input_type"] == "password"
    assert password["show_icon"] is True
    assert submit["form_action_type"] == "submit"
    assert submit["behaviors"][0]["value"]["token"] == "one-time-token"


def test_extract_email_bind_card_action_keeps_tenant_and_user_scope() -> None:
    app = TenantApp("tenant-1", "app-1", "secret", None, None, "bot-1", None)
    payload = {
        "event": {
            "operator": {"tenant_key": "tenant-1", "open_id": "user-1"},
            "context": {"open_chat_id": "chat-1", "open_message_id": "message-1"},
            "action": {
                "tag": "button",
                "value": {"action": "email_bind", "token": "token-1"},
                "form_value": {
                    "email_account": "user@example.com",
                    "email_password": "secret",
                },
            },
        }
    }

    action = extract_email_bind_card_action(payload, app)

    assert action == {
        "tenant_key": "tenant-1",
        "app_id": "app-1",
        "bot_code": "bot-1",
        "open_id": "user-1",
        "chat_id": "chat-1",
        "message_id": "message-1",
        "token": "token-1",
        "email": "user@example.com",
        "password": "secret",
    }


def test_extract_email_bind_card_action_rejects_other_tenant() -> None:
    app = TenantApp("tenant-1", "app-1", "secret", None, None, "bot-1", None)
    payload = {
        "event": {
            "operator": {"tenant_key": "tenant-2", "open_id": "user-1"},
            "context": {"open_chat_id": "chat-1", "open_message_id": "message-1"},
            "action": {
                "tag": "button",
                "value": {"action": "email_bind", "token": "token-1"},
                "form_value": {
                    "email_account": "user@example.com",
                    "email_password": "secret",
                },
            },
        }
    }

    assert extract_email_bind_card_action(payload, app) is None


def test_card_callback_frames_reach_the_sdk_dispatcher() -> None:
    seen: list[bytes] = []

    class Handler:
        def _do_without_validation(self, payload: bytes) -> None:
            seen.append(payload)

    client = object.__new__(CardCallbackWsClient)
    client._event_handler = Handler()
    client._write_message = AsyncMock()
    frame = Frame(SeqID=1, LogID=1, service=1, method=1, payload=b'{"schema":"2.0"}')
    for key, value in ((HEADER_TYPE, "card"), (HEADER_SUM, "1"), (HEADER_SEQ, "0")):
        header = frame.headers.add()
        header.key = key
        header.value = value

    asyncio.run(client._handle_data_frame(frame))

    assert seen == [b'{"schema":"2.0"}']
    client._write_message.assert_awaited_once()


def test_successful_card_binding_consumes_token_and_updates_card(monkeypatch: pytest.MonkeyPatch) -> None:
    app = TenantApp("tenant-1", "app-1", "secret", None, None, "bot-1", None)
    action = {
        "tenant_key": "tenant-1",
        "app_id": "app-1",
        "bot_code": "bot-1",
        "open_id": "user-1",
        "chat_id": "chat-1",
        "message_id": "message-1",
        "token": "token-1",
        "email": "user@example.com",
        "password": "secret",
    }
    scope = {key: action[key] for key in ("tenant_key", "app_id", "bot_code", "open_id", "chat_id")}
    account = {"id": 1}
    bind = AsyncMock(return_value=account)
    consume = AsyncMock()
    sync = AsyncMock(return_value=3)
    update = AsyncMock(return_value=True)
    send = AsyncMock()
    monkeypatch.setattr("app.feishu_ws.get_bind_scope", AsyncMock(return_value=scope))
    monkeypatch.setattr("app.feishu_ws.bind_email_account", bind)
    monkeypatch.setattr("app.feishu_ws.consume_bind_token", consume)
    monkeypatch.setattr("app.feishu_ws.initial_sync", sync)
    monkeypatch.setattr("app.feishu_ws.update_card", update)
    monkeypatch.setattr("app.feishu_ws.send_card", send)

    asyncio.run(process_email_bind_card(app, action))

    bind.assert_awaited_once()
    assert bind.await_args.args[1:] == ("user@example.com", "secret")
    consume.assert_awaited_once_with("token-1")
    sync.assert_awaited_once_with(account)
    assert "首次同步新增 3 封邮件" in update.await_args.args[2]["body"]["elements"][0]["content"]
    send.assert_not_awaited()


def test_wrong_card_password_keeps_token_for_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    app = TenantApp("tenant-1", "app-1", "secret", None, None, "bot-1", None)
    action = {
        "tenant_key": "tenant-1",
        "app_id": "app-1",
        "bot_code": "bot-1",
        "open_id": "user-1",
        "chat_id": "chat-1",
        "message_id": "message-1",
        "token": "token-1",
        "email": "user@example.com",
        "password": "wrong",
    }
    scope = {key: action[key] for key in ("tenant_key", "app_id", "bot_code", "open_id", "chat_id")}
    consume = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr("app.feishu_ws.get_bind_scope", AsyncMock(return_value=scope))
    monkeypatch.setattr(
        "app.feishu_ws.bind_email_account",
        AsyncMock(side_effect=EmailAuthenticationError("authentication failed")),
    )
    monkeypatch.setattr("app.feishu_ws.consume_bind_token", consume)
    monkeypatch.setattr("app.feishu_ws.send_card", send)

    asyncio.run(process_email_bind_card(app, action))

    consume.assert_not_awaited()
    assert "邮箱账号或密码错误" in send.await_args.args[2]["body"]["elements"][0]["content"]


def test_password_fields_are_redacted_from_logs() -> None:
    assert "secret" not in redact_sensitive_text('password=secret')
    assert "secret" not in redact_sensitive_text('{"email_password":"secret"}')
    assert "part-two" not in redact_sensitive_text("UID=user;PWD={secret;part-two};Encrypt=yes")
