import json
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from app.db import fetch_one
from app.mcp_client import McpClient


logger = logging.getLogger("feishu_agent")


@dataclass
class Identity:
    tenant_key: str
    app_id: str
    open_id: str
    union_id: str | None = None
    user_id: str | None = None
    internal_username: str | None = None
    oa_user_id: int | None = None
    display_name: str | None = None
    permissions: dict[str, Any] = field(default_factory=dict)

    @property
    def is_known(self) -> bool:
        return bool(self.internal_username or self.oa_user_id)


async def resolve_identity(
    *,
    tenant_key: str,
    app_id: str,
    open_id: str,
    union_id: str | None,
    user_id: str | None,
) -> Identity:
    row = await fetch_one(
        """
        SELECT *
        FROM feishu_identity_mapping
        WHERE tenant_key = ? AND app_id = ? AND open_id = ? AND enabled = 1
        """,
        (tenant_key, app_id, open_id),
    )
    if not row:
        return Identity(
            tenant_key=tenant_key,
            app_id=app_id,
            open_id=open_id,
            union_id=union_id,
            user_id=user_id,
        )
    return Identity(
        tenant_key=tenant_key,
        app_id=app_id,
        open_id=open_id,
        union_id=row.get("union_id") or union_id,
        user_id=row.get("user_id") or user_id,
        internal_username=row.get("internal_username"),
        oa_user_id=row.get("oa_user_id"),
        display_name=row.get("display_name"),
    )


async def load_business_permissions(identity: Identity) -> Identity:
    client = McpClient()
    permissions: dict[str, Any] = {}

    try:
        raw = await call_permission_tool_with_retry(client, identity)
        if not raw:
            raise ValueError("permission_user_customers returned empty response")
        permission_payload = json.loads(raw)
        if not isinstance(permission_payload, dict) or not permission_payload:
            raise ValueError("permission_user_customers returned empty payload")
        permissions["permission_user_customers"] = compact_customer_permission(permission_payload)
        username = permissions["permission_user_customers"].get("username")
        if username and not identity.internal_username:
            identity.internal_username = str(username)
        if username and not identity.display_name:
            identity.display_name = str(username)
        logger.info(
            "permission_user_customers resolved: tenant_key=%s app_id=%s open_id=%s username=%s authorized=%s customer_count=%s row_count=%s",
            identity.tenant_key,
            identity.app_id,
            identity.open_id,
            permissions["permission_user_customers"].get("username"),
            permissions["permission_user_customers"].get("authorized"),
            permissions["permission_user_customers"].get("customerCount"),
            permissions["permission_user_customers"].get("rowCount"),
        )
    except Exception as exc:  # noqa: BLE001
        permissions["permission_user_customers_error"] = str(exc)
        logger.warning(
            "permission_user_customers unresolved: tenant_key=%s app_id=%s open_id=%s error=%s",
            identity.tenant_key,
            identity.app_id,
            identity.open_id,
            exc,
        )

    identity.permissions = permissions
    return identity


async def call_permission_tool_with_retry(client: McpClient, identity: Identity) -> str:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            raw = await client.call_tool(
                "permission_user_customers",
                {"feishuOpenId": identity.open_id, "tenantKey": identity.tenant_key},
            )
            if raw:
                return raw
            last_error = ValueError("empty response")
        except Exception as exc:  # noqa: BLE001
            last_error = exc

        logger.warning(
            "permission_user_customers attempt %s failed: tenant_key=%s app_id=%s open_id=%s error=%s",
            attempt,
            identity.tenant_key,
            identity.app_id,
            identity.open_id,
            last_error,
        )
        await asyncio.sleep(0.3 * attempt)

    raise ValueError(f"permission_user_customers failed after retries: {last_error}")


def compact_customer_permission(payload: dict[str, Any]) -> dict[str, Any]:
    customer_short_names = payload.get("customerShortNames") or []
    rows = payload.get("rows") or []
    return {
        "feishuOpenId": payload.get("feishuOpenId"),
        "username": payload.get("username"),
        "authorized": payload.get("authorized"),
        "customerCount": payload.get("customerCount") or len(customer_short_names),
        "customerShortNames": customer_short_names[:200],
        "message": payload.get("message"),
        "finalAnswer": payload.get("finalAnswer"),
        "replyPolicy": payload.get("replyPolicy"),
        "rowCount": payload.get("rowCount") or len(rows),
    }
