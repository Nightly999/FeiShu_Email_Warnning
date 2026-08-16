import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.settings import get_settings


logger = logging.getLogger("feishu_agent")


LOCAL_TOOL_NAMES = {"message", "cron", "fs", "bash"}


@lru_cache
def get_allowed_tool_names() -> set[str]:
    settings = get_settings()
    config_path = Path(settings.tools_config_path)
    if not config_path.exists():
        logger.warning("Tools config file does not exist: %s", config_path)
        return set()

    with config_path.open("r", encoding="utf-8") as file:
        config = json.load(file)

    configured = extract_allow_list(config)
    allowed = {
        normalize_tool_name(name)
        for name in configured
        if normalize_tool_name(name) not in LOCAL_TOOL_NAMES
    }

    ignored = sorted({normalize_tool_name(name) for name in configured} & LOCAL_TOOL_NAMES)
    if ignored:
        logger.info("Ignored non-MCP local tools from tools config: %s", ", ".join(ignored))
    return allowed


@lru_cache
def get_automation_tool_names() -> set[str]:
    settings = get_settings()
    config_path = Path(settings.tools_config_path)
    if not config_path.exists():
        return set()
    with config_path.open("r", encoding="utf-8") as file:
        config = json.load(file)
    tools_config = config.get("tools")
    configured = (
        tools_config.get("automation_allow")
        if isinstance(tools_config, dict)
        else None
    )
    if not isinstance(configured, list):
        logger.warning("No tools.automation_allow configured; scheduled Agent tools are disabled")
        return set()
    return {
        normalize_tool_name(str(name))
        for name in configured
        if normalize_tool_name(str(name)) not in LOCAL_TOOL_NAMES
    } & get_allowed_tool_names()


def extract_allow_list(config: dict[str, Any]) -> list[str]:
    tools_config = config.get("tools")
    if isinstance(tools_config, dict) and isinstance(tools_config.get("allow"), list):
        return [str(item) for item in tools_config["allow"]]

    mcp_config = config.get("mcp")
    servers = mcp_config.get("servers") if isinstance(mcp_config, dict) else None
    if isinstance(servers, dict):
        allow: list[str] = []
        for server_config in servers.values():
            if not isinstance(server_config, dict):
                continue
            server_tools = server_config.get("tools")
            if isinstance(server_tools, dict) and isinstance(server_tools.get("allow"), list):
                allow.extend(str(item) for item in server_tools["allow"])
            elif isinstance(server_config.get("allow"), list):
                allow.extend(str(item) for item in server_config["allow"])
        return allow

    return []


def normalize_tool_name(name: str) -> str:
    if "__" in name:
        return name.split("__", 1)[1]
    return name
