from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, TypeVar

from app.settings import get_settings


T = TypeVar("T")
_schema_lock = threading.Lock()
_schema_ready = False


def _pyodbc():
    try:
        import pyodbc
    except ImportError as exc:
        raise RuntimeError("pyodbc 未安装，无法连接邮件 SQL Server") from exc
    return pyodbc


def _connection_string() -> str:
    settings = get_settings()
    required = {
        "EMAIL_SQLSERVER_SERVER": settings.email_sqlserver_server,
        "EMAIL_SQLSERVER_USER": settings.email_sqlserver_user,
        "EMAIL_SQLSERVER_PASSWORD": settings.email_sqlserver_password,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"邮件 SQL Server 缺少配置：{', '.join(missing)}")
    return ";".join(
        [
            f"DRIVER={_odbc_value(settings.email_sqlserver_driver)}",
            f"SERVER={_odbc_value(f'{settings.email_sqlserver_server},{settings.email_sqlserver_port}')}",
            f"DATABASE={_odbc_value(settings.email_sqlserver_database)}",
            f"UID={_odbc_value(settings.email_sqlserver_user)}",
            f"PWD={_odbc_value(settings.email_sqlserver_password)}",
            f"Encrypt={'yes' if settings.email_sqlserver_encrypt else 'no'}",
            "TrustServerCertificate="
            + ("yes" if settings.email_sqlserver_trust_server_certificate else "no"),
        ]
    )


def _odbc_value(value: str) -> str:
    return "{" + value.replace("}", "}}") + "}"


def _connect():
    global _schema_ready
    pyodbc = _pyodbc()
    connection = pyodbc.connect(_connection_string(), timeout=10)
    if not _schema_ready:
        with _schema_lock:
            if not _schema_ready:
                sql_path = Path(__file__).resolve().parents[1] / "schema" / "email_sqlserver_tables.sql"
                for batch in sql_path.read_text(encoding="utf-8").split("\nGO"):
                    if batch.strip():
                        connection.execute(batch)
                connection.commit()
                _schema_ready = True
    return connection


@contextmanager
def _open_connection():
    connection = _connect()
    try:
        yield connection
    finally:
        connection.close()


async def _run(function: Callable[..., T], *args: Any) -> T:
    return await asyncio.to_thread(function, *args)


async def get_email_account(tenant_key: str, app_id: str, open_id: str) -> dict[str, Any] | None:
    return await _run(_get_email_account, tenant_key, app_id, open_id)


def _get_email_account(tenant_key: str, app_id: str, open_id: str) -> dict[str, Any] | None:
    with _open_connection() as connection:
        cursor = connection.execute(
            """
            SELECT * FROM asi.email_account
            WHERE tenant_key = ? AND app_id = ? AND open_id = ? AND enabled = 1
            """,
            tenant_key,
            app_id,
            open_id,
        )
        return _row(cursor)


async def upsert_email_account(
    *,
    tenant_key: str,
    app_id: str,
    bot_code: str | None,
    open_id: str,
    chat_id: str,
    email_address: str,
    password_ciphertext: str,
) -> dict[str, Any]:
    return await _run(
        _upsert_email_account,
        tenant_key,
        app_id,
        bot_code,
        open_id,
        chat_id,
        email_address,
        password_ciphertext,
    )


def _upsert_email_account(
    tenant_key: str,
    app_id: str,
    bot_code: str | None,
    open_id: str,
    chat_id: str,
    email_address: str,
    password_ciphertext: str,
) -> dict[str, Any]:
    settings = get_settings()
    with _open_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE asi.email_account
            SET bot_code = ?, chat_id = ?, email_address = ?, password_ciphertext = ?,
                pop3_host = ?, pop3_port = ?, enabled = 1, last_sync_error = NULL,
                updated_at = SYSUTCDATETIME()
            WHERE tenant_key = ? AND app_id = ? AND open_id = ?
            """,
            bot_code,
            chat_id,
            email_address,
            password_ciphertext,
            settings.email_pop3_host,
            settings.email_pop3_port,
            tenant_key,
            app_id,
            open_id,
        )
        if cursor.rowcount == 0:
            connection.execute(
                """
                INSERT INTO asi.email_account (
                    tenant_key, app_id, bot_code, open_id, chat_id, email_address,
                    password_ciphertext, pop3_host, pop3_port, retention_days
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tenant_key,
                app_id,
                bot_code,
                open_id,
                chat_id,
                email_address,
                password_ciphertext,
                settings.email_pop3_host,
                settings.email_pop3_port,
                settings.email_default_retention_days,
            )
        connection.commit()
        cursor = connection.execute(
            "SELECT * FROM asi.email_account WHERE tenant_key = ? AND app_id = ? AND open_id = ?",
            tenant_key,
            app_id,
            open_id,
        )
        row = _row(cursor)
        if not row:
            raise RuntimeError("邮箱绑定保存失败")
        return row


async def disable_email_account(tenant_key: str, app_id: str, open_id: str) -> bool:
    return await _run(_disable_email_account, tenant_key, app_id, open_id)


def _disable_email_account(tenant_key: str, app_id: str, open_id: str) -> bool:
    with _open_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE asi.email_account SET enabled = 0, updated_at = SYSUTCDATETIME()
            WHERE tenant_key = ? AND app_id = ? AND open_id = ? AND enabled = 1
            """,
            tenant_key,
            app_id,
            open_id,
        )
        connection.commit()
        return cursor.rowcount > 0


async def update_retention(account_id: int, days: int) -> None:
    await _run(_execute, "UPDATE asi.email_account SET retention_days = ?, updated_at = SYSUTCDATETIME() WHERE id = ?", days, account_id)


async def known_uidls(account_id: int) -> set[str]:
    return await _run(_known_uidls, account_id)


def _known_uidls(account_id: int) -> set[str]:
    with _open_connection() as connection:
        rows = connection.execute(
            "SELECT pop3_uidl FROM asi.email_message WHERE email_account_id = ?", account_id
        ).fetchall()
        return {str(row[0]) for row in rows}


async def save_messages(account_id: int, messages: list[dict[str, Any]]) -> int:
    return await _run(_save_messages, account_id, messages)


def _save_messages(account_id: int, messages: list[dict[str, Any]]) -> int:
    inserted = 0
    with _open_connection() as connection:
        for item in messages:
            cursor = connection.execute(
                """
                INSERT INTO asi.email_message (
                    email_account_id, pop3_uidl, message_id, references_header, in_reply_to,
                    subject, sender_name, sender_address, to_json, cc_json, sent_at,
                    received_at, text_body, html_body, attachments_json
                )
                SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM asi.email_message
                    WHERE email_account_id = ? AND pop3_uidl = ?
                )
                """,
                account_id,
                item["pop3_uidl"],
                item["message_id"],
                item["references_header"],
                item["in_reply_to"],
                item["subject"],
                item["sender_name"],
                item["sender_address"],
                json.dumps(item["to"], ensure_ascii=False),
                json.dumps(item["cc"], ensure_ascii=False),
                item["sent_at"],
                item["received_at"],
                item["text_body"],
                item["html_body"],
                json.dumps(item["attachments"], ensure_ascii=False),
                account_id,
                item["pop3_uidl"],
            )
            inserted += max(0, cursor.rowcount)
        connection.commit()
    return inserted


async def update_sync_result(account_id: int, error: str | None = None) -> None:
    await _run(
        _execute,
        "UPDATE asi.email_account SET last_sync_at = SYSUTCDATETIME(), last_sync_error = ?, updated_at = SYSUTCDATETIME() WHERE id = ?",
        (error or None),
        account_id,
    )


async def list_recent_messages(account_id: int, lookback_hours: int, limit: int = 100) -> list[dict[str, Any]]:
    return await _run(_list_recent_messages, account_id, lookback_hours, limit)


def _list_recent_messages(account_id: int, lookback_hours: int, limit: int) -> list[dict[str, Any]]:
    cutoff = datetime.utcnow() - timedelta(hours=max(1, lookback_hours))
    with _open_connection() as connection:
        cursor = connection.execute(
            f"""
            SELECT TOP {max(1, min(limit, 100))}
                m.*, a.summary, a.importance, a.requires_attention, a.relation_type,
                a.todos_json, a.possible_owner, a.deadline, a.risks_json
            FROM asi.email_message m
            LEFT JOIN asi.email_analysis a ON a.email_message_id = m.id
            WHERE m.email_account_id = ? AND (m.sent_at IS NULL OR m.sent_at >= ?)
            ORDER BY COALESCE(m.sent_at, m.received_at) DESC, m.id DESC
            """,
            account_id,
            cutoff,
        )
        return _rows(cursor)


async def save_analysis(message_id: int, analysis: dict[str, Any], model_name: str, retention_days: int) -> None:
    await _run(_save_analysis, message_id, analysis, model_name, retention_days)


def _save_analysis(message_id: int, analysis: dict[str, Any], model_name: str, retention_days: int) -> None:
    expires_at = datetime.utcnow() + timedelta(days=max(1, retention_days))
    with _open_connection() as connection:
        connection.execute("DELETE FROM asi.email_analysis WHERE email_message_id = ?", message_id)
        connection.execute(
            """
            INSERT INTO asi.email_analysis (
                email_message_id, summary, importance, requires_attention, relation_type,
                todos_json, possible_owner, deadline, risks_json, model_name, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            message_id,
            analysis["summary"],
            analysis["importance"],
            bool(analysis["requires_attention"]),
            analysis["relation_type"],
            json.dumps(analysis["todos"], ensure_ascii=False),
            analysis.get("possible_owner"),
            analysis.get("deadline"),
            json.dumps(analysis["risks"], ensure_ascii=False),
            model_name,
            expires_at,
        )
        connection.execute(
            "UPDATE asi.email_message SET analysis_status = N'success', analysis_failed_reason = NULL WHERE id = ?",
            message_id,
        )
        connection.commit()


async def mark_analysis_failed(message_id: int, error: str) -> None:
    await _run(
        _execute,
        "UPDATE asi.email_message SET analysis_status = N'failed', analysis_failed_reason = ? WHERE id = ?",
        error[:1000],
        message_id,
    )


async def create_push_logs(message_ids: list[int], push_type: str, task_ref: str, run_ref: str) -> None:
    await _run(_create_push_logs, message_ids, push_type, task_ref, run_ref)


def _create_push_logs(message_ids: list[int], push_type: str, task_ref: str, run_ref: str) -> None:
    with _open_connection() as connection:
        for message_id in message_ids:
            connection.execute(
                """
                INSERT INTO asi.email_push_log (email_message_id, push_type, task_ref, run_ref, status)
                SELECT ?, ?, ?, ?, N'pending'
                WHERE NOT EXISTS (
                    SELECT 1 FROM asi.email_push_log
                    WHERE email_message_id = ? AND push_type = ? AND task_ref = ? AND run_ref = ?
                )
                """,
                message_id,
                push_type,
                task_ref,
                run_ref,
                message_id,
                push_type,
                task_ref,
                run_ref,
            )
        connection.commit()


async def successful_scheduled_message_ids(account_id: int) -> set[int]:
    return await _run(_successful_scheduled_message_ids, account_id)


def _successful_scheduled_message_ids(account_id: int) -> set[int]:
    with _open_connection() as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT p.email_message_id
            FROM asi.email_push_log p
            JOIN asi.email_message m ON m.id = p.email_message_id
            WHERE m.email_account_id = ? AND p.push_type = N'scheduled' AND p.status = N'success'
            """,
            account_id,
        ).fetchall()
        return {int(row[0]) for row in rows}


async def finalize_push_logs(run_ref: str, success: bool, error: str | None = None) -> None:
    await _run(_finalize_push_logs, run_ref, success, error)


def _finalize_push_logs(run_ref: str, success: bool, error: str | None) -> None:
    status = "success" if success else "failed"
    with _open_connection() as connection:
        connection.execute(
            """
            UPDATE asi.email_push_log
            SET status = ?, error = ?,
                pushed_at = CASE WHEN ? = N'success' THEN SYSUTCDATETIME() ELSE NULL END
            WHERE run_ref = ? AND status = N'pending'
            """,
            status,
            error,
            status,
            run_ref,
        )
        connection.execute(
            """
            UPDATE m SET push_status = ?, push_failed_reason = ?
            FROM asi.email_message m
            JOIN asi.email_push_log p ON p.email_message_id = m.id
            WHERE p.run_ref = ?
            """,
            status,
            error,
            run_ref,
        )
        connection.commit()


async def cleanup_expired_email_data() -> None:
    await _run(_cleanup_expired_email_data)


def _cleanup_expired_email_data() -> None:
    with _open_connection() as connection:
        connection.execute("DELETE FROM asi.email_analysis WHERE expires_at <= SYSUTCDATETIME()")
        connection.execute(
            """
            UPDATE m SET text_body = NULL, html_body = NULL
            FROM asi.email_message m
            JOIN asi.email_account a ON a.id = m.email_account_id
            WHERE m.received_at < DATEADD(day, -a.retention_days, SYSUTCDATETIME())
            """
        )
        connection.commit()


def _execute(sql: str, *params: Any) -> None:
    with _open_connection() as connection:
        connection.execute(sql, *params)
        connection.commit()


def _row(cursor) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if not row:
        return None
    columns = [item[0] for item in cursor.description]
    return dict(zip(columns, row, strict=True))


def _rows(cursor) -> list[dict[str, Any]]:
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
