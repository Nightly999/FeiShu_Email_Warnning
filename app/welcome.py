import logging
from typing import Any

from app.email_repository import get_email_account
from app.feishu import TenantApp, send_card
from app.feishu_cards import build_welcome_card
from app.settings import get_settings


logger = logging.getLogger("welcome")


async def send_chat_guide(app: TenantApp, event: dict[str, Any]) -> bool:
    settings = get_settings()
    if not settings.feishu_reply_enabled or not settings.email_feature_enabled:
        return False

    chat_id = str(event.get("chat_id") or "")
    open_id = str(event.get("open_id") or "")
    if not chat_id or not open_id:
        logger.warning(
            "Skip chat guide: missing chat/open id bot_code=%s chat_id=%s open_id=%s",
            app.bot_code,
            chat_id,
            open_id,
        )
        return False

    try:
        logged_in = bool(
            await get_email_account(event["tenant_key"], event["app_id"], open_id)
        )
        message_id = await send_card(
            app, chat_id, build_welcome_card(logged_in=logged_in)
        )
        if not message_id:
            raise RuntimeError("Feishu chat guide send returned empty message_id")
    except Exception:
        logger.exception(
            "Failed to send chat guide: bot_code=%s chat_id=%s open_id=%s",
            app.bot_code,
            chat_id,
            open_id,
        )
        return False
    return True
