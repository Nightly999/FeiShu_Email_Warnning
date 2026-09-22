import logging
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from app.db import open_db
from app.email_repository import get_email_account
from app.email_service import _binding_result
from app.feishu import TenantApp, send_card
from app.feishu_cards import build_welcome_card
from app.settings import get_settings


logger = logging.getLogger("welcome")
WELCOME_TIMEZONE = timezone(timedelta(hours=8))


async def send_daily_welcome_once(app: TenantApp, event: dict[str, Any]) -> bool:
    if not get_settings().feishu_reply_enabled:
        logger.info(
            "Skip welcome: reply disabled bot_code=%s chat_id=%s open_id=%s",
            app.bot_code,
            event.get("chat_id"),
            event.get("open_id"),
        )
        return False

    chat_id = str(event.get("chat_id") or "")
    open_id = str(event.get("open_id") or "")
    if not chat_id or not open_id:
        logger.warning(
            "Skip welcome: missing chat/open id bot_code=%s chat_id=%s open_id=%s",
            app.bot_code,
            chat_id,
            open_id,
        )
        return False

    needs_login = False
    if get_settings().email_feature_enabled:
        try:
            needs_login = await get_email_account(event["tenant_key"], event["app_id"], open_id) is None
        except Exception:
            logger.exception("Failed to check email binding on chat entry: bot_code=%s open_id=%s", app.bot_code, open_id)
            return False

    delivery_date = datetime.now(WELCOME_TIMEZONE).date().isoformat()
    if needs_login:
        delivery_date += ":email_login"
    claim = (
        event["tenant_key"],
        event["app_id"],
        event.get("bot_code"),
        chat_id,
        open_id,
        delivery_date,
    )
    async with open_db() as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO welcome_delivery (
              tenant_key, app_id, bot_code, chat_id, open_id, delivery_date
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            claim,
        )
        cursor = await db.execute("SELECT changes()")
        row = await cursor.fetchone()
        inserted = int(row[0] or 0)
    if not inserted:
        logger.info(
            "Skip welcome: already delivered today bot_code=%s chat_id=%s open_id=%s date=%s",
            app.bot_code,
            chat_id,
            open_id,
            delivery_date,
        )
        return False

    try:
        card = (await _binding_result(event, replacing=False)).card if needs_login else build_welcome_card()
        message_id = await send_card(app, chat_id, card)
        if not message_id:
            raise RuntimeError("Feishu welcome card send returned empty message_id")
    except Exception:
        await _clear_welcome_claim(
            tenant_key=event["tenant_key"],
            app_id=event["app_id"],
            chat_id=chat_id,
            open_id=open_id,
            delivery_date=delivery_date,
        )
        logger.exception(
            "Failed to send welcome message: bot_code=%s chat_id=%s open_id=%s",
            app.bot_code,
            chat_id,
            open_id,
        )
        return False

    logger.info(
        "Welcome card sent: bot_code=%s chat_id=%s open_id=%s message_id=%s",
        app.bot_code,
        chat_id,
        open_id,
        message_id,
    )
    return True


async def _clear_welcome_claim(
    *,
    tenant_key: str,
    app_id: str,
    chat_id: str,
    open_id: str,
    delivery_date: str,
) -> None:
    async with open_db() as db:
        await db.execute(
            """
            DELETE FROM welcome_delivery
            WHERE tenant_key = ?
              AND app_id = ?
              AND chat_id = ?
              AND open_id = ?
              AND delivery_date = ?
            """,
            (tenant_key, app_id, chat_id, open_id, delivery_date),
        )
