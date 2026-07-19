from __future__ import annotations

import json
import unittest
from pathlib import Path

try:
    from a2a.types import AgentCard, SendMessageRequest
    from google.protobuf.json_format import ParseDict
except ImportError:
    AgentCard = None
    SendMessageRequest = None
    ParseDict = None


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "contracts" / "fixtures" / "a2a"
EXTENSION_URI = "https://phenomenoner.github.io/a2a-superhub/ext/shared-memory/v1"


@unittest.skipIf(AgentCard is None, "install the contracts extra for official A2A parsing")
class OfficialA2AContractTests(unittest.TestCase):
    def test_agent_card_parses_with_official_sdk_proto(self) -> None:
        payload = json.loads((FIXTURES / "agent-card.json").read_text(encoding="utf-8"))
        card = ParseDict(payload, AgentCard(), ignore_unknown_fields=False)
        self.assertEqual("1.0", card.supported_interfaces[0].protocol_version)
        self.assertEqual("JSONRPC", card.supported_interfaces[0].protocol_binding)
        self.assertIn(EXTENSION_URI, [item.uri for item in card.capabilities.extensions])
        self.assertTrue(card.default_input_modes)
        self.assertTrue(card.default_output_modes)
        self.assertTrue(card.skills)
        extension_doc = (ROOT / "docs" / "ext" / "shared-memory" / "v1.md").read_text(encoding="utf-8")
        self.assertIn(EXTENSION_URI, extension_doc)

    def test_send_message_and_part_member_names_parse_with_official_sdk(self) -> None:
        payload = json.loads((FIXTURES / "send-message.json").read_text(encoding="utf-8"))
        request = ParseDict(payload, SendMessageRequest(), ignore_unknown_fields=False)
        self.assertEqual("msg_contract_001", request.message.message_id)
        self.assertEqual({"text", "raw", "url", "data"}, {part.WhichOneof("content") for part in request.message.parts})
        for raw_part in payload["message"]["parts"]:
            self.assertNotIn("kind", raw_part)


if __name__ == "__main__":
    unittest.main()
