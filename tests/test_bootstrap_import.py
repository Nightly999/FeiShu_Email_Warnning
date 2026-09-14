from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiosqlite

from app.bootstrap import import_feishu_apps


class FeishuAppImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_app_identity_can_change_bot_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "feishu_apps.json"
            config_path.write_text(
                json.dumps(
                    {
                        "apps": {
                            "new-code": {
                                "tenantKey": "tenant",
                                "appId": "app",
                                "appSecret": "new-secret",
                                "name": "Renamed bot",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            async with aiosqlite.connect(":memory:") as db:
                db.row_factory = aiosqlite.Row
                await db.execute(
                    """
                    CREATE TABLE feishu_tenant_app (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      tenant_key TEXT NOT NULL,
                      app_id TEXT NOT NULL,
                      app_secret TEXT NOT NULL,
                      encrypt_key TEXT,
                      verification_token TEXT,
                      bot_code TEXT NOT NULL UNIQUE,
                      bot_name TEXT,
                      enabled INTEGER NOT NULL DEFAULT 1,
                      UNIQUE (tenant_key, app_id)
                    )
                    """
                )
                await db.execute(
                    """
                    INSERT INTO feishu_tenant_app (
                      tenant_key, app_id, app_secret, bot_code, bot_name
                    ) VALUES ('tenant', 'app', 'old-secret', 'old-code', 'Old bot')
                    """
                )
                with patch(
                    "app.bootstrap.get_settings",
                    return_value=SimpleNamespace(
                        feishu_apps_config_path=str(config_path)
                    ),
                ):
                    await import_feishu_apps(db)

                cursor = await db.execute(
                    "SELECT * FROM feishu_tenant_app WHERE tenant_key = 'tenant' "
                    "AND app_id = 'app'"
                )
                row = await cursor.fetchone()

        self.assertEqual(row["bot_code"], "new-code")
        self.assertEqual(row["app_secret"], "new-secret")
        self.assertEqual(row["bot_name"], "Renamed bot")
        self.assertEqual(row["enabled"], 1)


if __name__ == "__main__":
    unittest.main()
