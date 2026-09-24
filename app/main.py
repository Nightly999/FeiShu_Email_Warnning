import hmac
import logging

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.bootstrap import bootstrap
from app.builtin_commands import handle_builtin_text_command
from app.feishu import (
    decrypt_event,
    event_scope_matches,
    get_tenant_app_by_bot_code,
    is_timestamp_fresh,
    normalize_event,
    reply_card,
    reply_message,
    update_card,
    verify_signature,
)
from app.event_dedup import claim_event, finish_event
from app.feishu_cards import build_answer_card, build_processing_card, should_use_card
from app.graph import run_agent
from app.logging_security import install_sensitive_log_filter
from app.memory.sessions import get_active_session_id
from app.multi_intent import split_multi_intent_commands
from app.reply_context import hydrate_reply_context
from app.settings import get_settings


app = FastAPI(title="Feishu Multi-Tenant LangGraph Agent", version="0.1.0")


@app.on_event("startup")
async def on_startup() -> None:
    install_sensitive_log_filter()
    await bootstrap()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/feishu/events/{bot_code}")
async def feishu_events(
    bot_code: str,
    request: Request,
    x_lark_request_timestamp: str | None = Header(default=None),
    x_lark_request_nonce: str | None = Header(default=None),
    x_lark_signature: str | None = Header(default=None),
) -> JSONResponse:
    tenant_app = await get_tenant_app_by_bot_code(bot_code)
    if not tenant_app:
        raise HTTPException(status_code=404, detail=f"Unknown bot_code: {bot_code}")

    raw_body = await request.body()
    payload = await request.json()

    if tenant_app.encrypt_key:
        if not all((x_lark_request_timestamp, x_lark_request_nonce, x_lark_signature)):
            raise HTTPException(status_code=401, detail="Missing Feishu signature headers")
        if not is_timestamp_fresh(
            x_lark_request_timestamp or "",
            max_age_seconds=get_settings().feishu_signature_max_age_seconds,
        ):
            raise HTTPException(status_code=401, detail="Expired Feishu request")
        ok = verify_signature(
            raw_body=raw_body,
            timestamp=x_lark_request_timestamp or "",
            nonce=x_lark_request_nonce or "",
            signature=x_lark_signature,
            encrypt_key=tenant_app.encrypt_key,
        )
        if not ok:
            raise HTTPException(status_code=401, detail="Invalid Feishu signature")

    if "encrypt" in payload:
        if not tenant_app.encrypt_key:
            raise HTTPException(status_code=400, detail="Encrypted event requires encrypt_key")
        payload = decrypt_event(payload["encrypt"], tenant_app.encrypt_key)

    if tenant_app.verification_token:
        header = payload.get("header") or {}
        supplied_token = str(header.get("token") or payload.get("token") or "")
        if not hmac.compare_digest(supplied_token, tenant_app.verification_token):
            raise HTTPException(status_code=401, detail="Invalid Feishu verification token")

    if not event_scope_matches(payload, tenant_app):
        logging.getLogger("feishu_http").warning("Rejected callback with mismatched tenant scope")
        raise HTTPException(status_code=403, detail="Feishu tenant scope mismatch")

    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge")})

    event = normalize_event(payload, tenant_app)
    if not event.get("open_id") or not event.get("text"):
        return JSONResponse({"status": "ignored"})

    message_id = event.get("message_id")
    if message_id and not await claim_event(
        tenant_key=event["tenant_key"], app_id=event["app_id"], message_id=message_id
    ):
        return JSONResponse({"status": "duplicate"})

    try:
        progress_message_id = None
        if event.get("message_id"):
            progress_message_id = await reply_card(
                tenant_app, event["message_id"], build_processing_card(event["text"])
            )

        session_id = await get_active_session_id(
            tenant_key=event["tenant_key"],
            app_id=event["app_id"],
            open_id=event["open_id"],
            chat_id=event.get("chat_id"),
            bot_code=event.get("bot_code"),
        )
        event["_session_id"] = session_id
        await hydrate_reply_context(tenant_app, event)
        event["_reply_message_id"] = progress_message_id
        commands = await split_multi_intent_commands(event["text"])
        for index, command_text in enumerate(commands):
            child_event = {**event, "text": command_text}
            child_progress_id = progress_message_id if index == 0 else None
            handled = await handle_builtin_text_command(
                tenant_app, child_event, child_progress_id
            )
            if handled:
                continue

            result = await run_agent(child_event)
            answer = result.content
            if event.get("message_id"):
                answer_card = build_answer_card(command_text, answer, status=result.status)
                if child_progress_id:
                    updated = await update_card(
                        tenant_app, child_progress_id, answer_card
                    )
                    if not updated:
                        await reply_card(tenant_app, event["message_id"], answer_card)
                elif should_use_card(answer):
                    await reply_card(tenant_app, event["message_id"], answer_card)
                else:
                    await reply_message(tenant_app, event["message_id"], answer)

        if message_id:
            await finish_event(
                tenant_key=event["tenant_key"], app_id=event["app_id"], message_id=message_id
            )
        return JSONResponse({"status": "ok"})
    except Exception as exc:
        if message_id:
            await finish_event(
                tenant_key=event["tenant_key"],
                app_id=event["app_id"],
                message_id=message_id,
                error=str(exc),
            )
        logging.getLogger("feishu_http").exception("Failed to process Feishu callback")
        raise HTTPException(status_code=503, detail="Message processing temporarily unavailable") from exc
