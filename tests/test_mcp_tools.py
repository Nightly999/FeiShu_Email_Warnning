from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app.mcp_tools import apply_business_pagination_defaults


class McpToolPaginationDefaultsTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_size_is_clamped_to_each_tool_schema(self) -> None:
        tools = [
            {
                "function": {
                    "name": "factory_list",
                    "parameters": {
                        "properties": {
                            "page": {"type": "integer"},
                            "pageSize": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 10,
                            },
                        }
                    },
                }
            }
        ]

        with patch(
            "app.mcp_tools.get_mcp_openai_tools", AsyncMock(return_value=tools)
        ):
            arguments = await apply_business_pagination_defaults(
                "factory_list", {"pageSize": 999}
            )

        self.assertEqual(arguments["page"], 1)
        self.assertEqual(arguments["pageSize"], 10)


if __name__ == "__main__":
    unittest.main()
