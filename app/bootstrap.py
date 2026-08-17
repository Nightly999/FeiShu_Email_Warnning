import asyncio
import json
from pathlib import Path

from app.db import open_db
from app.settings import get_settings


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS feishu_tenant_app (
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
);

CREATE TABLE IF NOT EXISTS feishu_identity_mapping (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_key TEXT NOT NULL,
  app_id TEXT NOT NULL,
  open_id TEXT NOT NULL,
  union_id TEXT,
  user_id TEXT,
  internal_username TEXT,
  oa_user_id INTEGER,
  display_name TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  UNIQUE (tenant_key, app_id, open_id)
);

CREATE TABLE IF NOT EXISTS agent_audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL,
  tenant_key TEXT NOT NULL,
  app_id TEXT NOT NULL,
  open_id TEXT NOT NULL,
  internal_username TEXT,
  bot_code TEXT,
  message_id TEXT,
  chat_id TEXT,
  tool_name TEXT,
  permission_result TEXT,
  user_message TEXT,
  tool_args TEXT,
  tool_result_summary TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS feishu_token_cache (
  cache_key TEXT PRIMARY KEY,
  access_token TEXT NOT NULL,
  expire_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS feishu_permission_cache (
  cache_key TEXT PRIMARY KEY,
  tenant_key TEXT NOT NULL,
  app_id TEXT NOT NULL,
  open_id TEXT NOT NULL,
  username TEXT NOT NULL,
  payload TEXT NOT NULL,
  expire_at INTEGER NOT NULL,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_tool_result_cache (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL,
  tenant_key TEXT NOT NULL,
  app_id TEXT NOT NULL,
  open_id TEXT NOT NULL,
  bot_code TEXT,
  message_id TEXT,
  chat_id TEXT,
  tool_name TEXT NOT NULL,
  tool_args TEXT,
  tool_result TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS scheduled_task (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_name TEXT,
  tenant_key TEXT NOT NULL,
  app_id TEXT NOT NULL,
  bot_code TEXT,
  chat_id TEXT NOT NULL,
  chat_type TEXT,
  open_id TEXT NOT NULL,
  schedule_type TEXT NOT NULL,
  run_at TEXT,
  daily_time TEXT,
  interval_minutes INTEGER,
  prompt TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  last_run_at TEXT,
  next_run_at TEXT,
  locked_until TEXT,
  last_error TEXT,
  execution_mode TEXT NOT NULL DEFAULT 'reminder',
  timeout_seconds INTEGER NOT NULL DEFAULT 120,
  max_retries INTEGER NOT NULL DEFAULT 5,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
  last_status TEXT,
  last_run_id TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS scheduled_task_run (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL UNIQUE,
  task_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  output_summary TEXT,
  error TEXT,
  FOREIGN KEY (task_id) REFERENCES scheduled_task(id)
);

CREATE INDEX IF NOT EXISTS idx_scheduled_task_run_history
ON scheduled_task_run (task_id, id DESC);

CREATE TABLE IF NOT EXISTS processed_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_key TEXT NOT NULL,
  app_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  status TEXT NOT NULL,
  last_error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE (tenant_key, app_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_processed_event_updated
ON processed_event (updated_at);

CREATE TABLE IF NOT EXISTS export_context (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_key TEXT NOT NULL,
  app_id TEXT NOT NULL,
  open_id TEXT NOT NULL,
  chat_id TEXT,
  session_id TEXT NOT NULL,
  source_type TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  source_name TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_export_context_lookup
ON export_context (tenant_key, app_id, open_id, chat_id, session_id, id);

CREATE TABLE IF NOT EXISTS business_list_cursor (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_key TEXT NOT NULL,
  app_id TEXT NOT NULL,
  open_id TEXT NOT NULL,
  chat_scope TEXT NOT NULL,
  session_id TEXT NOT NULL,
  result_id INTEGER NOT NULL,
  tool_name TEXT,
  current_page INTEGER NOT NULL DEFAULT 1,
  total_rows INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (tenant_key, app_id, open_id, chat_scope, session_id),
  FOREIGN KEY (result_id) REFERENCES agent_tool_result_cache(id)
);

CREATE INDEX IF NOT EXISTS idx_business_list_cursor_result
ON business_list_cursor (result_id);

CREATE TABLE IF NOT EXISTS conversation_turn (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_key TEXT NOT NULL,
  app_id TEXT NOT NULL,
  bot_code TEXT,
  chat_id TEXT,
  open_id TEXT NOT NULL,
  session_id TEXT,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  metadata TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_conversation_turn_lookup
ON conversation_turn (tenant_key, app_id, open_id, chat_id, session_id, id);

CREATE TABLE IF NOT EXISTS conversation_session (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL UNIQUE,
  tenant_key TEXT NOT NULL,
  app_id TEXT NOT NULL,
  bot_code TEXT,
  chat_id TEXT,
  open_id TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  title TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  ended_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_conversation_session_active
ON conversation_session (tenant_key, app_id, open_id, chat_id, active, id);

CREATE TABLE IF NOT EXISTS agent_memory (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_key TEXT NOT NULL,
  app_id TEXT NOT NULL,
  bot_code TEXT,
  chat_id TEXT,
  open_id TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT 'user_chat',
  memory_type TEXT NOT NULL DEFAULT 'explicit',
  content TEXT NOT NULL,
  metadata TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agent_memory_lookup
ON agent_memory (tenant_key, app_id, open_id, chat_id, enabled, updated_at);

CREATE TABLE IF NOT EXISTS uploaded_file (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_key TEXT NOT NULL,
  app_id TEXT NOT NULL,
  bot_code TEXT,
  chat_id TEXT,
  open_id TEXT NOT NULL,
  session_id TEXT,
  message_id TEXT,
  file_key TEXT,
  file_name TEXT,
  resource_type TEXT,
  local_path TEXT NOT NULL,
  file_size INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_uploaded_file_lookup
ON uploaded_file (tenant_key, app_id, open_id, chat_id, session_id, id);

CREATE TABLE IF NOT EXISTS welcome_delivery (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_key TEXT NOT NULL,
  app_id TEXT NOT NULL,
  bot_code TEXT,
  chat_id TEXT NOT NULL,
  open_id TEXT NOT NULL,
  delivery_date TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (tenant_key, app_id, chat_id, open_id, delivery_date)
);

CREATE INDEX IF NOT EXISTS idx_welcome_delivery_lookup
ON welcome_delivery (tenant_key, app_id, chat_id, open_id, delivery_date);
"""


SAMPLE_SQL = """
INSERT OR IGNORE INTO feishu_tenant_app (
  tenant_key, app_id, app_secret, encrypt_key, verification_token, bot_code, bot_name
) VALUES (
  'sample_tenant_key',
  'cli_sample_app_id',
  'replace_with_app_secret',
  'replace_with_encrypt_key',
  'replace_with_verification_token',
  'asi_inventory_bot',
  'ASI库存查询助手'
);

INSERT OR IGNORE INTO feishu_identity_mapping (
  tenant_key, app_id, open_id, internal_username, display_name
) VALUES (
  'sample_tenant_key',
  'cli_sample_app_id',
  'ou_sample_open_id',
  'demo_user',
  '演示用户'
);
"""


async def bootstrap() -> None:
    async with open_db() as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.executescript(SCHEMA_SQL)
        await ensure_audit_columns(db)
        await ensure_memory_columns(db)
        await ensure_conversation_session_integrity(db)
        await ensure_uploaded_file_columns(db)
        await ensure_scheduler_columns(db)
        if get_settings().app_env.lower() in {"dev", "local", "test"}:
            await db.executescript(SAMPLE_SQL)
        else:
            await db.execute(
                "UPDATE feishu_tenant_app SET enabled = 0 WHERE app_id = 'cli_sample_app_id'"
            )
            await db.execute(
                "UPDATE feishu_identity_mapping SET enabled = 0 "
                "WHERE app_id = 'cli_sample_app_id'"
            )
        await import_feishu_apps(db)


async def ensure_audit_columns(db) -> None:
    cursor = await db.execute("PRAGMA table_info(agent_audit_log)")
    columns = {row[1] for row in await cursor.fetchall()}
    for column in ("bot_code", "message_id", "chat_id"):
        if column not in columns:
            await db.execute(f"ALTER TABLE agent_audit_log ADD COLUMN {column} TEXT")


async def ensure_memory_columns(db) -> None:
    cursor = await db.execute("PRAGMA table_info(conversation_turn)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "session_id" not in columns:
        await db.execute("ALTER TABLE conversation_turn ADD COLUMN session_id TEXT")


async def ensure_conversation_session_integrity(db) -> None:
    await db.execute(
        """
        UPDATE conversation_session
        SET active = 0,
            ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP)
        WHERE id IN (
          SELECT older.id
          FROM conversation_session AS older
          JOIN conversation_session AS newer
            ON newer.tenant_key = older.tenant_key
           AND newer.app_id = older.app_id
           AND newer.open_id = older.open_id
           AND (newer.chat_id = older.chat_id OR (
                 newer.chat_id IS NULL AND older.chat_id IS NULL
               ))
           AND newer.active = 1
           AND newer.id > older.id
          WHERE older.active = 1
        )
        """
    )
    await db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_session_one_active
        ON conversation_session (
          tenant_key, app_id, open_id, COALESCE(chat_id, '<null-chat>')
        )
        WHERE active = 1
        """
    )


async def ensure_uploaded_file_columns(db) -> None:
    cursor = await db.execute("PRAGMA table_info(uploaded_file)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "session_id" not in columns:
        await db.execute("ALTER TABLE uploaded_file ADD COLUMN session_id TEXT")


async def ensure_scheduler_columns(db) -> None:
    cursor = await db.execute("PRAGMA table_info(scheduled_task)")
    columns = {row[1] for row in await cursor.fetchall()}
    column_definitions = {
        "locked_until": "TEXT",
        "last_error": "TEXT",
        "execution_mode": "TEXT NOT NULL DEFAULT 'reminder'",
        "timeout_seconds": "INTEGER NOT NULL DEFAULT 120",
        "max_retries": "INTEGER NOT NULL DEFAULT 5",
        "consecutive_failures": "INTEGER NOT NULL DEFAULT 0",
        "timezone": "TEXT NOT NULL DEFAULT 'Asia/Shanghai'",
        "last_status": "TEXT",
        "last_run_id": "TEXT",
        "chat_type": "TEXT",
        "task_name": "TEXT",
        "interval_minutes": "INTEGER",
    }
    for column, definition in column_definitions.items():
        if column not in columns:
            await db.execute(
                f"ALTER TABLE scheduled_task ADD COLUMN {column} {definition}"
            )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_scheduled_task_due
        ON scheduled_task (enabled, next_run_at, locked_until, id)
        """
    )
    await db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_task_name_unique
        ON scheduled_task (tenant_key, app_id, chat_id, open_id, task_name)
        WHERE task_name IS NOT NULL
        """
    )


async def import_feishu_apps(db) -> None:
    settings = get_settings()
    config_path = Path(settings.feishu_apps_config_path)
    if not config_path.exists():
        return

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    apps = payload.get("apps", payload)
    configured_codes = list(apps.keys())
    for bot_code, app_config in apps.items():
        tenant_key = app_config.get("tenantKey") or app_config.get("tenant_key") or ""
        app_id = app_config["appId"]
        app_secret = app_config["appSecret"]
        name = app_config.get("name")
        encrypt_key = app_config.get("encryptKey") or app_config.get("encrypt_key")
        verification_token = app_config.get("verificationToken") or app_config.get("verification_token")

        await db.execute(
            """
            INSERT INTO feishu_tenant_app (
              tenant_key, app_id, app_secret, encrypt_key, verification_token, bot_code, bot_name, enabled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(bot_code) DO UPDATE SET
              tenant_key = excluded.tenant_key,
              app_id = excluded.app_id,
              app_secret = excluded.app_secret,
              encrypt_key = excluded.encrypt_key,
              verification_token = excluded.verification_token,
              bot_name = excluded.bot_name,
              enabled = 1
            """,
            (tenant_key, app_id, app_secret, encrypt_key, verification_token, bot_code, name),
        )

    # Config file is the source of truth: disable bots removed from the file.
    if configured_codes:
        placeholders = ", ".join("?" for _ in configured_codes)
        await db.execute(
            f"""
            UPDATE feishu_tenant_app
            SET enabled = 0
            WHERE bot_code NOT IN ({placeholders})
            """,
            configured_codes,
        )
    else:
        await db.execute("UPDATE feishu_tenant_app SET enabled = 0")


if __name__ == "__main__":
    asyncio.run(bootstrap())


def main() -> None:
    asyncio.run(bootstrap())
