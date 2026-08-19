import logging
from dataclasses import dataclass, field
from typing import Any

from app.db import fetch_one


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
