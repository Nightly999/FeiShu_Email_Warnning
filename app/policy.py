from dataclasses import dataclass
from typing import Any

from app.identity import Identity
from app.settings import get_settings
from app.tool_config import get_allowed_tool_names


@dataclass
class PolicyResult:
    allowed: bool
    reason: str


def check_agent_access(identity: Identity) -> PolicyResult:
    if not identity.open_id:
        return PolicyResult(False, "缺少飞书用户 open_id")

    customer_permission = identity.permissions.get("permission_user_customers") or {}
    permission_error = identity.permissions.get("permission_user_customers_error")
    strict = get_settings().permission_fail_closed
    if customer_permission.get("authorized") is True:
        return PolicyResult(True, "ok")
    if customer_permission.get("authorized") is False:
        return PolicyResult(False, customer_permission.get("finalAnswer") or "没有业务权限")
    if permission_error and strict:
        return PolicyResult(False, "暂时无法连接权限服务，请稍后再试或联系信息管理中心。")
    if strict:
        return PolicyResult(False, "权限服务未返回明确授权结果，请联系信息管理中心。")

    if not identity.is_known:
        if permission_error:
            return PolicyResult(False, "暂时无法连接权限服务，请稍后再试或联系信息管理中心。")
        if customer_permission.get("rowCount"):
            return PolicyResult(False, "权限服务已返回授权数据，但缺少账号字段，请联系信息管理中心检查 permission_user_customers 返回格式。")
        return PolicyResult(False, "该飞书账号未绑定内部账号，请联系信息管理中心")

    return PolicyResult(True, "ok")


def check_tool_access(identity: Identity, tool_name: str, arguments: dict[str, Any]) -> PolicyResult:
    if tool_name not in get_allowed_tool_names():
        return PolicyResult(False, f"工具 {tool_name} 未在允许列表中")

    customer_permission = identity.permissions.get("permission_user_customers") or {}
    if get_settings().permission_fail_closed and customer_permission.get("authorized") is not True:
        return PolicyResult(False, customer_permission.get("finalAnswer") or "没有明确的业务授权")
    if tool_name.startswith(
        (
            "inventory_",
            "production_",
            "purchase_",
            "sample_",
            "sampleshedule_",
            "fr_",
            "cfa_",
        )
    ):
        if customer_permission.get("authorized") is False:
            return PolicyResult(False, customer_permission.get("finalAnswer") or "没有业务权限")

    return PolicyResult(True, "ok")


def inject_identity_args(identity: Identity, arguments: dict[str, Any]) -> dict[str, Any]:
    protected = dict(arguments or {})
    protected["feishuOpenId"] = identity.open_id
    protected["FeishuOpenId"] = identity.open_id
    protected["tenantKey"] = identity.tenant_key
    protected["appId"] = identity.app_id
    protected["internalUsername"] = identity.internal_username
    return protected
