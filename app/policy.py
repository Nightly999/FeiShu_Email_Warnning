from dataclasses import dataclass
from typing import Any

from app.identity import Identity
from app.tool_config import get_allowed_tool_names


@dataclass
class PolicyResult:
    allowed: bool
    reason: str


def check_agent_access(identity: Identity) -> PolicyResult:
    if not identity.open_id:
        return PolicyResult(False, "缺少飞书用户 open_id")
    return PolicyResult(True, "ok")


def check_tool_access(identity: Identity, tool_name: str, arguments: dict[str, Any]) -> PolicyResult:
    if tool_name not in get_allowed_tool_names():
        return PolicyResult(False, f"工具 {tool_name} 未在允许列表中")

    return PolicyResult(True, "由 MCP 工具根据 open_id 校验业务权限")


def inject_identity_args(identity: Identity, arguments: dict[str, Any]) -> dict[str, Any]:
    protected = dict(arguments or {})
    protected["feishuOpenId"] = identity.open_id
    protected["FeishuOpenId"] = identity.open_id
    protected["tenantKey"] = identity.tenant_key
    protected["appId"] = identity.app_id
    protected["internalUsername"] = identity.internal_username
    return protected
