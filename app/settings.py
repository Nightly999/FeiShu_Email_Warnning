from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "dev"
    app_database_path: str = "./data/agent.db"

    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    openai_base_url: str | None = None
    openai_fallback_models: str = ""
    vision_model: str = "qwen3-vl-plus"
    video_model: str = "qwen3.7-plus"
    models_config_path: str = "./config/models.local.json"
    model_request_timeout_seconds: int = 60
    agent_recursion_limit: int = 12
    max_video_upload_bytes: int = 20 * 1024 * 1024
    max_file_upload_bytes: int = 25 * 1024 * 1024

    mcp_base_url: str = "http://127.0.0.1:8000/mcp/"
    mcp_timeout_seconds: int = 60
    mcp_server_name: str = "erp-sqlserver"
    tools_config_path: str = "./config/tools.local.json"

    feishu_base_url: str = "https://open.feishu.cn"
    feishu_reply_enabled: bool = False
    feishu_signature_max_age_seconds: int = 300
    feishu_event_workers: int = 8
    feishu_event_queue_size: int = 64
    feishu_apps_config_path: str = "./config/feishu_apps.local.json"
    agent_skills_path: str = "./app/agent_skills"
    short_memory_turns: int = 8
    long_memory_limit: int = 12
    permission_fail_closed: bool = True
    sqlite_busy_timeout_ms: int = 10_000
    scheduler_poll_seconds: int = 15
    scheduler_task_timeout_seconds: int = 120
    scheduler_max_retries: int = 5
    scheduler_concurrency: int = 4
    scheduler_list_page_size: int = 5
    scheduler_history_page_size: int = 5
    business_list_page_size: int = 20
    business_list_max_columns: int = 20

    email_feature_enabled: bool = False
    email_bind_token_ttl_seconds: int = 600
    email_pop3_host: str = "mail.asiansha.com"
    email_pop3_port: int = 995
    email_pop3_timeout_seconds: int = 30
    email_initial_lookback_hours: int = 48
    email_initial_max_messages: int = 100
    email_default_retention_days: int = 7
    email_min_retention_days: int = 1
    email_max_retention_days: int = 365
    email_max_body_chars: int = 50_000
    email_credential_key: str = ""
    email_sqlserver_driver: str = "ODBC Driver 18 for SQL Server"
    email_sqlserver_server: str = ""
    email_sqlserver_port: int = 1433
    email_sqlserver_database: str = "AI_outlook_email"
    email_sqlserver_user: str = ""
    email_sqlserver_password: str = ""
    email_sqlserver_encrypt: bool = False
    email_sqlserver_trust_server_certificate: bool = True

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
