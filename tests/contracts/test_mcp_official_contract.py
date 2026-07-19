from __future__ import annotations

import json
import unittest
from pathlib import Path

try:
    from mcp.shared.version import LATEST_PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS
    from mcp.types import InitializeRequest, InitializeResult, ResourceTemplate, Tool
except ImportError:
    InitializeRequest = None
    InitializeResult = None
    ResourceTemplate = None
    Tool = None
    LATEST_PROTOCOL_VERSION = None
    SUPPORTED_PROTOCOL_VERSIONS = []


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipIf(InitializeRequest is None, "install the contracts extra for official MCP parsing")
class OfficialMcpContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = json.loads((ROOT / "schemas" / "mcp-memory-v1.contract.json").read_text(encoding="utf-8"))

    def test_initialize_negotiation_uses_current_supported_protocol(self) -> None:
        self.assertEqual("2025-11-25", self.contract["protocolVersion"])
        self.assertEqual(LATEST_PROTOCOL_VERSION, self.contract["protocolVersion"])
        self.assertIn(self.contract["protocolVersion"], SUPPORTED_PROTOCOL_VERSIONS)
        request = InitializeRequest.model_validate(self.contract["initializeRequest"])
        result = InitializeResult.model_validate(self.contract["initializeResult"])
        self.assertEqual("initialize", request.method)
        self.assertIsNotNone(result.capabilities.tools)
        self.assertIsNotNone(result.capabilities.resources)

    def test_tools_and_resources_parse_with_official_sdk_types(self) -> None:
        tools = [Tool.model_validate(item) for item in self.contract["tools"]]
        resources = [ResourceTemplate.model_validate(item) for item in self.contract["resourceTemplates"]]
        self.assertEqual(len(tools), len({tool.name for tool in tools}))
        self.assertEqual(2, len(resources))
        for tool in tools:
            self.assertIsNotNone(tool.annotations)
            if tool.name in {"memory_note_read", "memory_search", "memory_inbox", "memory_wakeup"}:
                self.assertTrue(tool.annotations.readOnlyHint, tool.name)
            else:
                self.assertFalse(tool.annotations.readOnlyHint, tool.name)


if __name__ == "__main__":
    unittest.main()
