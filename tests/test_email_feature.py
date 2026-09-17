from __future__ import annotations

import asyncio
import base64
import os
import poplib
from unittest.mock import AsyncMock

import pytest
from lark_oapi.ws.const import HEADER_SEQ, HEADER_SUM, HEADER_TYPE
from lark_oapi.ws.pb.pbbp2_pb2 import Frame

from app.email_pop3 import EmailAuthenticationError, parse_message
from app.email_security import decrypt_email_password, encrypt_email_password
from app.email_service import (
    EmailPlan,
    _fallback_plan,
    _select_messages,
    _validated_plan,
    build_email_login_card,
    build_email_report,
    build_email_report_card,
    sync_analyze_report,
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

    hourly = _fallback_plan("分析最近12小时的前5封未处理邮件", settings)
    assert hourly.lookback_hours == 12
    assert hourly.limit == 5
    assert hourly.only_unprocessed is True


def test_explicit_numbers_override_inconsistent_model_plan() -> None:
    plan = _validated_plan(
        EmailPlan(lookback_hours=48, limit=3, only_unprocessed=False),
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
            EmailPlan(),
        )
    )

    assert result.status == "error"
    assert "大模型分析暂时不可用" in result.answer


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
            EmailPlan(),
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
        EmailPlan(),
    )

    assert card["header"]["title"]["content"] == "📬 AI 邮件智能简报"
    assert all(item["tag"] != "table" for item in card["body"]["elements"])
    content = "\n".join(
        item.get("content", "") for item in card["body"]["elements"]
    )
    assert "AI 摘要" in content
    assert "确认报价并回复" in content
    assert "逾期可能影响交付" in content


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
