import base64
import hashlib
import hmac
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.db import execute, fetch_all, fetch_one
from app.settings import get_settings


logger = logging.getLogger("feishu_api")


@dataclass
class TenantApp:
    tenant_key: str
    app_id: str
    app_secret: str
    encrypt_key: str | None
    verification_token: str | None
    bot_code: str
    bot_name: str | None


async def get_tenant_app_by_bot_code(bot_code: str) -> TenantApp | None:
    row = await fetch_one(
        "SELECT * FROM feishu_tenant_app WHERE bot_code = ? AND enabled = 1",
        (bot_code,),
    )
    if not row:
        return None
    return TenantApp(
        tenant_key=row["tenant_key"],
        app_id=row["app_id"],
        app_secret=row["app_secret"],
        encrypt_key=row.get("encrypt_key"),
        verification_token=row.get("verification_token"),
        bot_code=row["bot_code"],
        bot_name=row.get("bot_name"),
    )


async def list_enabled_tenant_apps() -> list[TenantApp]:
    rows = await fetch_all(
        """
        SELECT *
        FROM feishu_tenant_app
        WHERE enabled = 1
        ORDER BY bot_code
        """
    )
    return [
        TenantApp(
            tenant_key=row["tenant_key"],
            app_id=row["app_id"],
            app_secret=row["app_secret"],
            encrypt_key=row.get("encrypt_key"),
            verification_token=row.get("verification_token"),
            bot_code=row["bot_code"],
            bot_name=row.get("bot_name"),
        )
        for row in rows
    ]


def verify_signature(*, raw_body: bytes, timestamp: str, nonce: str, signature: str, encrypt_key: str) -> bool:
    body = raw_body.decode("utf-8")
    raw = f"{timestamp}{nonce}{encrypt_key}{body}"
    expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected, signature)


def is_timestamp_fresh(timestamp: str, *, max_age_seconds: int, now: int | None = None) -> bool:
    try:
        event_time = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = int(time.time()) if now is None else now
    return abs(current - event_time) <= max_age_seconds


def event_scope_matches(
    payload: dict[str, Any],
    tenant_app: TenantApp,
    *,
    allow_unconfigured_tenant: bool = False,
) -> bool:
    header = payload.get("header") or {}
    tenant_key = header.get("tenant_key") or payload.get("tenant_key")
    app_id = header.get("app_id") or payload.get("app_id")
    if tenant_app.tenant_key:
        tenant_matches = not tenant_key or tenant_key == tenant_app.tenant_key
    else:
        tenant_matches = allow_unconfigured_tenant and bool(tenant_key)
    return tenant_matches and (not app_id or app_id == tenant_app.app_id)


def decrypt_event(encrypt: str, encrypt_key: str) -> dict[str, Any]:
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    encrypted = base64.b64decode(encrypt)
    iv = encrypted[:16]
    ciphertext = encrypted[16:]
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    pad_len = padded[-1]
    plaintext = padded[:-pad_len]
    return json.loads(plaintext.decode("utf-8"))


def normalize_event(
    payload: dict[str, Any],
    tenant_app: TenantApp,
    *,
    trust_event_tenant: bool = False,
) -> dict[str, Any]:
    header = payload.get("header") or {}
    event = payload.get("event") or {}
    sender = event.get("sender") or {}
    sender_id = sender.get("sender_id") or event.get("operator_id") or {}
    message = event.get("message") or {}

    return {
        "tenant_key": tenant_app.tenant_key
        or (
            str(header.get("tenant_key") or payload.get("tenant_key") or "")
            if trust_event_tenant
            else ""
        ),
        "app_id": tenant_app.app_id,
        "bot_code": tenant_app.bot_code,
        "open_id": sender_id.get("open_id") or event.get("open_id"),
        "union_id": sender_id.get("union_id"),
        "user_id": sender_id.get("user_id"),
        "chat_id": message.get("chat_id") or event.get("chat_id"),
        "chat_type": message.get("chat_type") or event.get("chat_type"),
        "message_id": message.get("message_id"),
        "root_id": message.get("root_id"),
        "parent_id": message.get("parent_id"),
        "thread_id": message.get("thread_id"),
        "message_type": message.get("message_type"),
        "text": extract_text(message),
        "file_key": extract_resource_key(message),
        "file_name": extract_file_name(message),
        "resource_type": extract_resource_type(message),
    }


def extract_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if not content:
        return ""
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
            return str(parsed.get("text") or "").strip()
        except json.JSONDecodeError:
            return content.strip()
    if isinstance(content, dict):
        return str(content.get("text") or "").strip()
    return str(content).strip()


def extract_message_content(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return content if isinstance(content, dict) else {}


def extract_file_key(message: dict[str, Any]) -> str | None:
    content = extract_message_content(message)
    value = content.get("file_key") or content.get("fileKey")
    return str(value) if value else None


def extract_image_key(message: dict[str, Any]) -> str | None:
    content = extract_message_content(message)
    value = content.get("image_key") or content.get("imageKey")
    return str(value) if value else None


def extract_resource_key(message: dict[str, Any]) -> str | None:
    return extract_file_key(message) or extract_image_key(message)


def extract_resource_type(message: dict[str, Any]) -> str:
    return "image" if message.get("message_type") == "image" else "file"


def extract_file_name(message: dict[str, Any]) -> str | None:
    content = extract_message_content(message)
    value = content.get("file_name") or content.get("fileName") or content.get("name")
    return str(value) if value else None


async def get_tenant_access_token(app: TenantApp) -> str:
    cache_key = f"tenant-access:{app.app_id}"
    now = int(time.time())
    cached = await fetch_one(
        "SELECT access_token, expire_at FROM feishu_token_cache WHERE cache_key = ?",
        (cache_key,),
    )
    if cached and int(cached["expire_at"]) > now + 600:
        return cached["access_token"]

    settings = get_settings()
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{settings.feishu_base_url}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app.app_id, "app_secret": app.app_secret},
        )
        response.raise_for_status()
        data = response.json()

    if data.get("code") != 0:
        raise ValueError(f"Feishu token error: {data}")

    token = data["tenant_access_token"]
    expire = int(data.get("expire", 7200))
    await execute(
        """
        INSERT INTO feishu_token_cache (cache_key, access_token, expire_at)
        VALUES (?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE SET
          access_token = excluded.access_token,
          expire_at = excluded.expire_at
        """,
        (cache_key, token, now + expire),
    )
    return token


async def get_message(app: TenantApp, message_id: str) -> dict[str, Any] | None:
    token = await get_tenant_access_token(app)
    settings = get_settings()
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"{settings.feishu_base_url}/open-apis/im/v1/messages/{message_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        payload = response.json()
    if payload.get("code") not in (None, 0):
        raise ValueError(f"Feishu get message error: {payload}")
    items = (payload.get("data") or {}).get("items") or []
    return items[0] if items and isinstance(items[0], dict) else None


async def reply_message(app: TenantApp, message_id: str, text: str) -> str | None:
    return await reply_payload(
        app,
        message_id,
        msg_type="text",
        content={"text": text},
    )


async def reply_file(app: TenantApp, message_id: str, file_key: str) -> str | None:
    return await reply_payload(
        app,
        message_id,
        msg_type="file",
        content={"file_key": file_key},
    )


async def send_message(app: TenantApp, receive_id: str, *, msg_type: str, content: dict[str, Any]) -> str | None:
    settings = get_settings()
    if not settings.feishu_reply_enabled:
        return None

    token = await get_tenant_access_token(app)
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{settings.feishu_base_url}/open-apis/im/v1/messages",
            headers={"Authorization": f"Bearer {token}"},
            params={"receive_id_type": "chat_id"},
            json={
                "receive_id": receive_id,
                "msg_type": msg_type,
                "content": json.dumps(content, ensure_ascii=False),
            },
        )
        response.raise_for_status()
        data = response.json()
    if data.get("code") not in (0, None):
        raise ValueError(f"Feishu send message error: {data}")
    return ((data.get("data") or {}).get("message_id") or (data.get("data") or {}).get("messageId"))


async def send_card(
    app: TenantApp, receive_id: str, card: dict[str, Any]
) -> str | None:
    try:
        return await send_message(
            app,
            receive_id,
            msg_type="interactive",
            content=card,
        )
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Feishu card send rejected: status=%s body=%s",
            exc.response.status_code,
            exc.response.text[:1000],
        )
        return await send_message(
            app,
            receive_id,
            msg_type="text",
            content={"text": extract_card_text(card)},
        )


async def reply_card(app: TenantApp, message_id: str, card: dict[str, Any]) -> str | None:
    try:
        return await reply_payload(
            app,
            message_id,
            msg_type="interactive",
            content=card,
        )
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Feishu card reply rejected: status=%s body=%s",
            exc.response.status_code,
            exc.response.text[:1000],
        )
        fallback = extract_card_text(card)
        return await reply_message(app, message_id, fallback)


async def update_card(app: TenantApp, message_id: str, card: dict[str, Any]) -> bool:
    settings = get_settings()
    if not settings.feishu_reply_enabled:
        return False

    token = await get_tenant_access_token(app)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.patch(
                f"{settings.feishu_base_url}/open-apis/im/v1/messages/{message_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "msg_type": "interactive",
                    "content": json.dumps(card, ensure_ascii=False),
                },
            )
            response.raise_for_status()
        return True
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Feishu card update rejected: message_id=%s status=%s body=%s",
            message_id,
            exc.response.status_code,
            exc.response.text[:1000],
        )
        return False


async def upload_file(app: TenantApp, path: Path, *, file_type: str = "stream") -> str:
    settings = get_settings()
    token = await get_tenant_access_token(app)
    async with httpx.AsyncClient(timeout=60) as client:
        with path.open("rb") as file:
            response = await client.post(
                f"{settings.feishu_base_url}/open-apis/im/v1/files",
                headers={"Authorization": f"Bearer {token}"},
                data={"file_type": file_type, "file_name": path.name},
                files={"file": (path.name, file, "application/octet-stream")},
            )
        response.raise_for_status()
        data = response.json()
    if data.get("code") != 0:
        raise ValueError(f"Feishu file upload error: {data}")
    file_key = (data.get("data") or {}).get("file_key") or (data.get("data") or {}).get("fileKey")
    if not file_key:
        raise ValueError(f"Feishu file upload missing file_key: {data}")
    return str(file_key)


async def download_message_file(
    app: TenantApp,
    *,
    message_id: str,
    file_key: str,
    save_path: Path,
    resource_type: str = "file",
) -> Path:
    settings = get_settings()
    token = await get_tenant_access_token(app)
    save_path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream(
                "GET",
                f"{settings.feishu_base_url}/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
                headers={"Authorization": f"Bearer {token}"},
                params={"type": resource_type},
            ) as response:
                response.raise_for_status()
                content_length = int(response.headers.get("content-length") or 0)
                if content_length > settings.max_file_upload_bytes:
                    raise ValueError("文件超过系统允许的大小限制")
                with save_path.open("wb") as file:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > settings.max_file_upload_bytes:
                            raise ValueError("文件超过系统允许的大小限制")
                        file.write(chunk)
    except Exception:
        save_path.unlink(missing_ok=True)
        raise
    return save_path


async def reply_payload(app: TenantApp, message_id: str, *, msg_type: str, content: dict[str, Any]) -> str | None:
    settings = get_settings()
    if not settings.feishu_reply_enabled:
        return None

    token = await get_tenant_access_token(app)
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{settings.feishu_base_url}/open-apis/im/v1/messages/{message_id}/reply",
            headers={"Authorization": f"Bearer {token}"},
            params={"receive_id_type": "open_id"},
            json={
                "msg_type": msg_type,
                "content": json.dumps(content, ensure_ascii=False),
            },
        )
        response.raise_for_status()
        data = response.json()
    return ((data.get("data") or {}).get("message_id") or (data.get("data") or {}).get("messageId"))


def extract_card_text(card: dict[str, Any]) -> str:
    parts: list[str] = []
    header = card.get("header") or {}
    title = header.get("title") or {}
    if title.get("content"):
        parts.append(_plain_card_text(str(title["content"])))

    elements = list(card.get("elements") or [])
    body = card.get("body") or {}
    elements.extend(body.get("elements") or [])
    for element in elements:
        if not isinstance(element, dict):
            continue
        if element.get("content"):
            parts.append(_plain_card_text(str(element["content"])))
        text = element.get("text")
        if isinstance(text, dict) and text.get("content"):
            parts.append(_plain_card_text(str(text["content"])))
        if element.get("tag") == "table":
            headers = [
                str(column.get("display_name") or column.get("name"))
                for column in element.get("columns") or []
            ]
            if headers:
                parts.append(" | ".join(headers))
            for row in (element.get("rows") or [])[:5]:
                if isinstance(row, dict):
                    parts.append(" | ".join(str(row.get(column.get("name"), "")) for column in element.get("columns") or []))
    return "\n\n".join(parts) or "已处理。"


def _plain_card_text(text: str) -> str:
    text = re.sub(r"</?font(?:\s+[^>]*)?>", "", text, flags=re.I)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"(?m)^#{1,6}\s+", "", text)
    return text.strip()
