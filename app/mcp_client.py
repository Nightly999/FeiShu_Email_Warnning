import json
from typing import Any

import httpx

from app.settings import get_settings


class McpClient:
    """Small JSON-RPC client for MCP Streamable HTTP servers."""

    def __init__(self) -> None:
        settings = get_settings()
        self.base_url = settings.mcp_base_url
        self.timeout = settings.mcp_timeout_seconds

    async def list_tools(self) -> list[dict[str, Any]]:
        payload = {
            "jsonrpc": "2.0",
            "id": "list-tools",
            "method": "tools/list",
            "params": {},
        }
        data = await self._post(payload)
        return data.get("result", {}).get("tools", [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        payload = {
            "jsonrpc": "2.0",
            "id": f"call-{name}",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        data = await self._post(payload)
        content = data.get("result", {}).get("content", [])
        if not content:
            return ""
        return "\n".join(str(item.get("text", "")) for item in content if item.get("type") == "text")

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.base_url, headers=headers, json=payload)
            response.raise_for_status()
            text = response.text.strip()
            if text.startswith("event:"):
                return _parse_sse_json(text)
            return response.json()


def _parse_sse_json(text: str) -> dict[str, Any]:
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line.removeprefix("data:").strip())
    raise ValueError("MCP SSE response did not contain a data line")
