import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.bootstrap import bootstrap
from app.event_dedup import claim_event, finish_event
from app.feishu import (
    TenantApp,
    event_scope_matches,
    is_timestamp_fresh,
    normalize_event,
)
from app.identity import Identity
from app.logging_security import redact_sensitive_text
from app.policy import check_agent_access
from app.settings import get_settings


def tenant_app() -> TenantApp:
    return TenantApp(
        tenant_key="trusted-tenant",
        app_id="trusted-app",
        app_secret="secret",
        encrypt_key="encrypt-key",
        verification_token="verification-token",
        bot_code="enterprise-bot",
        bot_name="Enterprise Bot",
    )


class SecurityBoundaryTests(unittest.TestCase):
    def tearDown(self) -> None:
        get_settings.cache_clear()

    def test_timestamp_replay_window(self) -> None:
        self.assertTrue(is_timestamp_fresh("1000", max_age_seconds=300, now=1200))
        self.assertFalse(is_timestamp_fresh("1000", max_age_seconds=300, now=1400))
        self.assertFalse(is_timestamp_fresh("invalid", max_age_seconds=300, now=1200))

    def test_event_scope_cannot_override_configured_tenant(self) -> None:
        payload = {
            "header": {"tenant_key": "attacker-tenant", "app_id": "attacker-app"},
            "event": {"sender": {"sender_id": {"open_id": "ou_example"}}},
        }
        self.assertFalse(event_scope_matches(payload, tenant_app()))
        event = normalize_event(payload, tenant_app())
        self.assertEqual(event["tenant_key"], "trusted-tenant")
        self.assertEqual(event["app_id"], "trusted-app")

    def test_authenticated_ws_event_can_supply_missing_tenant(self) -> None:
        app = tenant_app()
        app.tenant_key = ""
        payload = {
            "header": {"tenant_key": "feishu-tenant", "app_id": app.app_id},
            "event": {"sender": {"sender_id": {"open_id": "ou_example"}}},
        }
        self.assertFalse(event_scope_matches(payload, app))
        self.assertTrue(
            event_scope_matches(payload, app, allow_unconfigured_tenant=True)
        )
        event = normalize_event(payload, app, trust_event_tenant=True)
        self.assertEqual(event["tenant_key"], "feishu-tenant")

    def test_permission_errors_fail_closed(self) -> None:
        identity = Identity(
            tenant_key="trusted-tenant",
            app_id="trusted-app",
            open_id="ou_example",
            internal_username="known-user",
            permissions={"permission_user_customers_error": "upstream unavailable"},
        )
        self.assertFalse(check_agent_access(identity).allowed)

    def test_sensitive_log_values_are_redacted(self) -> None:
        value = (
            "wss://example/ws?access_key=abc&ticket=def "
            "open_id=ou_1234567890abcdef"
        )
        redacted = redact_sensitive_text(value)
        self.assertNotIn("access_key=abc", redacted)
        self.assertNotIn("ticket=def", redacted)
        self.assertNotIn("ou_1234567890abcdef", redacted)


class EventDeduplicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.previous_db_path = os.environ.get("APP_DATABASE_PATH")
        self.previous_apps_path = os.environ.get("FEISHU_APPS_CONFIG_PATH")
        self.previous_app_env = os.environ.get("APP_ENV")
        os.environ["APP_DATABASE_PATH"] = str(Path(self.temp_dir.name) / "agent.db")
        os.environ["FEISHU_APPS_CONFIG_PATH"] = str(
            Path(self.temp_dir.name) / "missing-apps.json"
        )
        os.environ["APP_ENV"] = "production"
        get_settings.cache_clear()
        await bootstrap()

    async def asyncTearDown(self) -> None:
        get_settings.cache_clear()
        restore_environment("APP_DATABASE_PATH", self.previous_db_path)
        restore_environment("FEISHU_APPS_CONFIG_PATH", self.previous_apps_path)
        restore_environment("APP_ENV", self.previous_app_env)
        self.temp_dir.cleanup()

    async def test_completed_message_cannot_be_claimed_twice(self) -> None:
        scope = {
            "tenant_key": "tenant",
            "app_id": "app",
            "message_id": "message-1",
        }
        self.assertTrue(await claim_event(**scope))
        await finish_event(**scope)
        self.assertFalse(await claim_event(**scope))

    async def test_failed_message_can_retry(self) -> None:
        scope = {
            "tenant_key": "tenant",
            "app_id": "app",
            "message_id": "message-2",
        }
        self.assertTrue(await claim_event(**scope))
        await finish_event(**scope, error="temporary failure")
        self.assertTrue(await claim_event(**scope))


class LegacyDatabaseMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_scheduler_table_is_upgraded_before_index_creation(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            db_path = Path(temp_dir) / "legacy.db"
            with closing(sqlite3.connect(db_path)) as db:
                db.execute(
                    """
                    CREATE TABLE scheduled_task (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      tenant_key TEXT NOT NULL,
                      app_id TEXT NOT NULL,
                      chat_id TEXT NOT NULL,
                      open_id TEXT NOT NULL,
                      enabled INTEGER NOT NULL DEFAULT 1,
                      next_run_at TEXT
                    )
                    """
                )

            previous_db_path = os.environ.get("APP_DATABASE_PATH")
            previous_apps_path = os.environ.get("FEISHU_APPS_CONFIG_PATH")
            previous_app_env = os.environ.get("APP_ENV")
            try:
                os.environ["APP_DATABASE_PATH"] = str(db_path)
                os.environ["FEISHU_APPS_CONFIG_PATH"] = str(
                    Path(temp_dir) / "missing-apps.json"
                )
                os.environ["APP_ENV"] = "production"
                get_settings.cache_clear()
                await bootstrap()

                with closing(sqlite3.connect(db_path)) as db:
                    columns = {row[1] for row in db.execute("PRAGMA table_info(scheduled_task)")}
                    indexes = {row[1] for row in db.execute("PRAGMA index_list(scheduled_task)")}
                    run_table = db.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'scheduled_task_run'"
                    ).fetchone()
                self.assertIn("locked_until", columns)
                self.assertIn("last_error", columns)
                self.assertIn("execution_mode", columns)
                self.assertIn("timeout_seconds", columns)
                self.assertIn("chat_type", columns)
                self.assertIn("task_name", columns)
                self.assertIn("interval_minutes", columns)
                self.assertIn("idx_scheduled_task_due", indexes)
                self.assertIn("idx_scheduled_task_name_unique", indexes)
                self.assertIsNotNone(run_table)
            finally:
                get_settings.cache_clear()
                restore_environment("APP_DATABASE_PATH", previous_db_path)
                restore_environment("FEISHU_APPS_CONFIG_PATH", previous_apps_path)
                restore_environment("APP_ENV", previous_app_env)


def restore_environment(key: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
