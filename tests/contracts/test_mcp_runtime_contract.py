from __future__ import annotations

import json
import unittest
from pathlib import Path

try:
    from mcp.server.lowlevel import NotificationOptions
except ImportError:
    NotificationOptions = None


ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TOOLS = {
    "memory_write",
    "memory_search",
    "memory_read",
    "memory_timeline",
    "memory_graph",
    "memory_wakeup",
    "memory_inbox",
    "memory_inbox_ack",
    "task_create",
    "task_status",
}


@unittest.skipIf(NotificationOptions is None, "install the mcp extra for runtime contract tests")
class McpRuntimeContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_exports_exact_public_tools_and_resources(self) -> None:
        from a2a_superhub.mcp_server import build_server, resource_templates, tool_definitions

        tools = tool_definitions()
        self.assertEqual(EXPECTED_TOOLS, {tool.name for tool in tools})
        self.assertEqual(10, len(tools))
        templates = resource_templates()
        self.assertEqual(
            {"memory://note/{id}", "memory://wakeup/{agent}"},
            {str(item.uriTemplate) for item in templates},
        )

        server, _subscriptions = build_server("http://127.0.0.1:8787", token=None)
        capabilities = server.get_capabilities(NotificationOptions(), {})
        self.assertIsNotNone(capabilities.tools)
        self.assertIsNotNone(capabilities.resources)
        self.assertTrue(capabilities.resources.subscribe)

    async def test_checked_in_contract_is_generated_from_runtime_definitions(self) -> None:
        from a2a_superhub.mcp_server import resource_templates, tool_definitions

        contract = json.loads((ROOT / "schemas" / "mcp-memory-v1.contract.json").read_text(encoding="utf-8"))
        runtime_tools = [item.model_dump(mode="json", by_alias=True, exclude_none=True) for item in tool_definitions()]
        runtime_resources = [
            item.model_dump(mode="json", by_alias=True, exclude_none=True) for item in resource_templates()
        ]
        self.assertEqual(contract["tools"], runtime_tools)
        self.assertEqual(contract["resourceTemplates"], runtime_resources)
        self.assertTrue(contract["initializeResult"]["capabilities"]["resources"]["subscribe"])

    def test_console_entry_point_is_declared(self) -> None:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('a2a-superhub-mcp = "a2a_superhub.mcp_server:main"', text)


if __name__ == "__main__":
    unittest.main()
