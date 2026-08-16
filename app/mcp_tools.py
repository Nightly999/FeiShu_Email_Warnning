import copy
import logging
from typing import Any

from app.mcp_client import McpClient
from app.settings import get_settings
from app.tool_config import get_allowed_tool_names


logger = logging.getLogger("feishu_agent")

IDENTITY_ARGUMENTS = {"feishuOpenId", "FeishuOpenId", "tenantKey", "appId", "internalUsername"}
_OPENAI_TOOL_CACHE: list[dict[str, Any]] | None = None


async def get_mcp_openai_tools() -> list[dict[str, Any]]:
    """Load MCP tools/list and expose approved tools as OpenAI-compatible tool schemas."""
    global _OPENAI_TOOL_CACHE
    if _OPENAI_TOOL_CACHE is None:
        mcp_tools = await McpClient().list_tools()
        allowed_tool_names = get_allowed_tool_names()
        _OPENAI_TOOL_CACHE = [
            to_openai_tool_schema(tool_info)
            for tool_info in mcp_tools
            if str(tool_info.get("name") or "") in allowed_tool_names
        ]
        logger.info(
            "Loaded %s approved MCP tools for model binding from %s: %s",
            len(_OPENAI_TOOL_CACHE),
            get_settings().tools_config_path,
            ", ".join(tool["function"]["name"] for tool in _OPENAI_TOOL_CACHE),
        )
    return _OPENAI_TOOL_CACHE


async def apply_business_pagination_defaults(
    tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Request one two-page Feishu table batch from MCP tools that support pageSize."""
    updated = dict(arguments)
    for tool in await get_mcp_openai_tools():
        function = tool.get("function") or {}
        if function.get("name") != tool_name:
            continue
        properties = (function.get("parameters") or {}).get("properties") or {}
        if "pageSize" in properties:
            page_size_schema = properties["pageSize"]
            minimum = int(page_size_schema.get("minimum", 1))
            maximum = int(
                page_size_schema.get(
                    "maximum", get_settings().business_list_page_size
                )
            )
            try:
                requested = int(
                    updated.get("pageSize", get_settings().business_list_page_size)
                )
            except (TypeError, ValueError):
                requested = get_settings().business_list_page_size
            page_size = min(max(requested, minimum), maximum)
            updated.setdefault("page", 1)
            updated["pageSize"] = page_size
        break
    return updated


def to_openai_tool_schema(tool_info: dict[str, Any]) -> dict[str, Any]:
    name = str(tool_info.get("name") or "")
    description = str(tool_info.get("description") or f"MCP tool {name}")
    input_schema = sanitize_input_schema(tool_info.get("inputSchema") or {})
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": input_schema,
        },
    }


def sanitize_input_schema(input_schema: dict[str, Any]) -> dict[str, Any]:
    schema = copy.deepcopy(input_schema) if isinstance(input_schema, dict) else {}
    schema.setdefault("type", "object")
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        schema["properties"] = {}
        properties = schema["properties"]

    for field_name in list(properties):
        if field_name in IDENTITY_ARGUMENTS:
            properties.pop(field_name, None)

    required = schema.get("required")
    if isinstance(required, list):
        schema["required"] = [
            field_name
            for field_name in required
            if isinstance(field_name, str) and field_name not in IDENTITY_ARGUMENTS
        ]

    schema["additionalProperties"] = True
    return schema
