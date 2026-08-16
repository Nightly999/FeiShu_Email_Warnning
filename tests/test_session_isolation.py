from __future__ import annotations

import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.bootstrap import bootstrap
from app.feishu_ws import ConversationSequencer
from app.files.repository import get_latest_uploaded_file, save_uploaded_file
from app.memory.repository import fetch_recent_turns
from app.memory.service import record_agent_exchange
from app.memory.sessions import create_new_session, get_active_session_id
from app.settings import get_settings


class StrictSessionIsolationTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_missing_chat_id_does_not_reuse_another_chat_session(self) -> None:
        common = {
            "tenant_key": "tenant",
            "app_id": "app",
            "open_id": "user",
            "bot_code": "bot",
        }
        chat_session = await get_active_session_id(**common, chat_id="chat-a")
        no_chat_session = await get_active_session_id(**common, chat_id=None)

        self.assertNotEqual(chat_session, no_chat_session)

    async def test_old_inflight_answer_stays_in_old_session_after_new(self) -> None:
        event = {
            "tenant_key": "tenant",
            "app_id": "app",
            "open_id": "user",
            "bot_code": "bot",
            "chat_id": "chat-a",
            "text": "旧会话中的慢查询",
            "message_id": "message-old",
        }
        old_session = await get_active_session_id(
            tenant_key="tenant",
            app_id="app",
            open_id="user",
            chat_id="chat-a",
            bot_code="bot",
        )
        event["_session_id"] = old_session
        new_session = await create_new_session(event)

        await record_agent_exchange(event, "旧查询稍后才完成")

        old_turns = await fetch_recent_turns(
            tenant_key="tenant",
            app_id="app",
            open_id="user",
            chat_id="chat-a",
            session_id=old_session,
            limit=10,
        )
        new_turns = await fetch_recent_turns(
            tenant_key="tenant",
            app_id="app",
            open_id="user",
            chat_id="chat-a",
            session_id=new_session,
            limit=10,
        )
        self.assertEqual([turn["content"] for turn in old_turns], [
            "旧会话中的慢查询",
            "旧查询稍后才完成",
        ])
        self.assertEqual(new_turns, [])

    async def test_uploaded_file_is_exactly_scoped_to_chat_and_session(self) -> None:
        file_path = self.root / "chat-a.xlsx"
        file_path.write_bytes(b"test")
        await save_uploaded_file(
            tenant_key="tenant",
            app_id="app",
            open_id="user",
            path=file_path,
            bot_code="bot",
            chat_id="chat-a",
            session_id="session-a",
            message_id="message",
            file_key="file-key",
            file_name=file_path.name,
            resource_type="file",
        )

        self.assertIsNone(
            await get_latest_uploaded_file(
                tenant_key="tenant",
                app_id="app",
                open_id="user",
                chat_id=None,
                session_id="session-a",
            )
        )
        self.assertIsNone(
            await get_latest_uploaded_file(
                tenant_key="tenant",
                app_id="app",
                open_id="user",
                chat_id="chat-a",
                session_id="session-b",
            )
        )


class ConversationSequencerTests(unittest.TestCase):
    def test_same_conversation_runs_in_received_ticket_order(self) -> None:
        sequencer = ConversationSequencer()
        key = ("tenant", "app", "chat")
        first_ticket = sequencer.issue(key)
        second_ticket = sequencer.issue(key)
        completed: list[int] = []

        def run(ticket: int) -> None:
            with sequencer.turn(key, ticket):
                completed.append(ticket)

        with ThreadPoolExecutor(max_workers=2) as executor:
            second = executor.submit(run, second_ticket)
            first = executor.submit(run, first_ticket)
            first.result(timeout=2)
            second.result(timeout=2)

        self.assertEqual(completed, [first_ticket, second_ticket])


if __name__ == "__main__":
    unittest.main()
