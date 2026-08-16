from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.bootstrap import bootstrap
from app.business_pagination import (
    parse_business_page_command,
    register_business_result,
    render_business_page,
)
from app.feishu_cards import build_answer_card
from app.identity import Identity
from app.policy import PolicyResult
from app.settings import get_settings
from app.tool_result_cache import save_tool_result


class BusinessPageCommandTests(unittest.TestCase):
    def test_conversation_page_commands_are_parsed(self) -> None:
        self.assertEqual(parse_business_page_command("下一页"), {"direction": "next"})
        self.assertEqual(
            parse_business_page_command("上一页"), {"direction": "previous"}
        )
        self.assertEqual(parse_business_page_command("第 3 页"), {"page": 3})
        self.assertEqual(parse_business_page_command("业务明细 第2页"), {"page": 2})
        self.assertEqual(
            parse_business_page_command("下一批。"), {"direction": "next"}
        )
        self.assertEqual(
            parse_business_page_command("往前翻一页"), {"direction": "previous"}
        )
        self.assertEqual(parse_business_page_command("跳到第4批"), {"page": 4})
        self.assertIsNone(parse_business_page_command("下一页是什么"))


class BusinessPaginationTests(unittest.IsolatedAsyncioTestCase):
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

    async def save_rows(self, rows: list[dict], *, request_id: str) -> int:
        result = json.dumps({"rows": rows}, ensure_ascii=False)
        result_id = await save_tool_result(
            request_id=request_id,
            tenant_key="tenant",
            app_id="app",
            open_id="user",
            bot_code="bot",
            message_id=request_id,
            chat_id="chat",
            tool_name="production_list",
            tool_args={},
            tool_result=result,
        )
        await register_business_result(
            tenant_key="tenant",
            app_id="app",
            open_id="user",
            chat_id="chat",
            session_id="session-a",
            result_id=result_id,
            tool_name="production_list",
            tool_result=result,
        )
        return result_id

    async def render(self, **overrides) -> str | None:
        params = {
            "tenant_key": "tenant",
            "app_id": "app",
            "open_id": "user",
            "chat_id": "chat",
            "session_id": "session-a",
        }
        params.update(overrides)
        return await render_business_page(**params)

    async def save_remote_page(self) -> int:
        result = json.dumps(
            {
                "rows": [{"编号": number} for number in range(1, 21)],
                "page": 1,
                "pageSize": 20,
                "totalCount": 45,
                "totalPages": 3,
            },
            ensure_ascii=False,
        )
        result_id = await save_tool_result(
            request_id="request-remote",
            tenant_key="tenant",
            app_id="app",
            open_id="user",
            bot_code="bot",
            message_id="message-remote",
            chat_id="chat",
            tool_name="sampleshedule_risk",
            tool_args={"page": 1, "pageSize": 20, "feishuOpenId": "stale-user"},
            tool_result=result,
        )
        row_count = await register_business_result(
            tenant_key="tenant",
            app_id="app",
            open_id="user",
            chat_id="chat",
            session_id="session-a",
            result_id=result_id,
            tool_name="sampleshedule_risk",
            tool_result=result,
        )
        self.assertEqual(row_count, 45)
        return result_id

    async def test_rows_are_paginated_and_cursor_tracks_next_page(self) -> None:
        rows = [
            {"生产单号": f"MO-{number:03d}", "状态": "进行中", "数量": number}
            for number in range(1, 24)
        ]
        await self.save_rows(rows, request_id="request-1")

        first = await self.render(page=1)
        second = await self.render(direction="next")
        boundary = await self.render(direction="next")

        self.assertIn("第 1/2 批，共 23 条；卡片内每页 10 条", first)
        self.assertIn("MO-001", first)
        self.assertIn("MO-020", first)
        self.assertNotIn("MO-021", first)
        self.assertIn("第 2/2 批，共 23 条；卡片内每页 10 条", second)
        self.assertIn("MO-021", second)
        self.assertIn("MO-023", second)
        self.assertIn("已经是最后一批", boundary)

        card = build_answer_card("查询生产明细", first)
        tables = [
            element
            for element in card["body"]["elements"]
            if element.get("tag") == "table"
        ]
        self.assertEqual(len(tables), 1)
        self.assertEqual(len(tables[0]["rows"]), 20)
        self.assertEqual(tables[0]["page_size"], 10)
        self.assertTrue(tables[0]["freeze_first_column"])

    async def test_card_can_show_more_than_six_columns(self) -> None:
        rows = [
            {f"字段{column}": f"值{column}" for column in range(1, 13)}
            for _ in range(2)
        ]
        await self.save_rows(rows, request_id="request-wide")

        page = await self.render(page=1)
        card = build_answer_card("查询宽表明细", page)
        table = next(
            element
            for element in card["body"]["elements"]
            if element.get("tag") == "table"
        )

        # Sequence column plus 12 business fields; Feishu renders the remainder
        # through horizontal scrolling.
        self.assertEqual(len(table["columns"]), 13)
        self.assertTrue(table["freeze_first_column"])

    async def test_new_result_replaces_old_cursor(self) -> None:
        await self.save_rows(
            [{"旧结果": number} for number in range(15)], request_id="request-old"
        )
        await self.render(page=2)
        await self.save_rows(
            [{"新结果": "A"}, {"新结果": "B"}], request_id="request-new"
        )

        page = await self.render()

        self.assertIn("第 1/1 批，共 2 条；卡片内每页 10 条", page)
        self.assertIn("新结果", page)
        self.assertNotIn("旧结果", page)

    async def test_cursor_is_isolated_by_session_chat_and_user(self) -> None:
        await self.save_rows([{"编号": number} for number in range(12)], request_id="request")

        self.assertIsNone(await self.render(session_id="session-after-new"))
        self.assertIsNone(await self.render(chat_id="another-chat"))
        self.assertIsNone(await self.render(open_id="another-user"))

    async def test_remote_page_calls_mcp_with_fresh_identity_and_updates_cursor(self) -> None:
        await self.save_remote_page()
        identity = Identity(tenant_key="tenant", app_id="app", open_id="user")
        second_result = json.dumps(
            {
                "rows": [{"编号": number} for number in range(21, 41)],
                "page": 2,
                "pageSize": 20,
                "totalCount": 45,
                "totalPages": 3,
            },
            ensure_ascii=False,
        )

        with (
            patch(
                "app.business_pagination.resolve_identity",
                AsyncMock(return_value=identity),
            ),
            patch(
                "app.business_pagination.load_business_permissions",
                AsyncMock(return_value=identity),
            ),
            patch(
                "app.business_pagination.check_agent_access",
                return_value=PolicyResult(True, "ok"),
            ),
            patch(
                "app.business_pagination.check_tool_access",
                return_value=PolicyResult(True, "ok"),
            ),
            patch(
                "app.business_pagination.McpClient.call_tool",
                AsyncMock(return_value=second_result),
            ) as call_tool,
        ):
            page = await self.render(direction="next")

        self.assertIn("第 2/3 批，共 45 条", page)
        self.assertIn("| 21 | 21 |", page)
        arguments = call_tool.await_args.args[1]
        self.assertEqual(arguments["page"], 2)
        self.assertEqual(arguments["pageSize"], 20)
        self.assertEqual(arguments["feishuOpenId"], "user")
        self.assertEqual(arguments["tenantKey"], "tenant")

    async def test_remote_page_rechecks_permission_before_calling_mcp(self) -> None:
        await self.save_remote_page()
        identity = Identity(tenant_key="tenant", app_id="app", open_id="user")

        with (
            patch(
                "app.business_pagination.resolve_identity",
                AsyncMock(return_value=identity),
            ),
            patch(
                "app.business_pagination.load_business_permissions",
                AsyncMock(return_value=identity),
            ),
            patch(
                "app.business_pagination.check_agent_access",
                return_value=PolicyResult(False, "权限已撤销"),
            ),
            patch(
                "app.business_pagination.McpClient.call_tool", new_callable=AsyncMock
            ) as call_tool,
        ):
            page = await self.render(direction="next")

        self.assertEqual(page, "权限已撤销")
        call_tool.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
