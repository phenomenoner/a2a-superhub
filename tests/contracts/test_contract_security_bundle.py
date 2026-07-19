from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ContractAndSecurityBundleTests(unittest.TestCase):
    def test_required_contract_artifacts_exist(self) -> None:
        required = (
            "schemas/memory-note-v1.schema.json",
            "schemas/principal-v1.schema.json",
            "schemas/memory-api-v1.schema.json",
            "schemas/mcp-memory-v1.contract.json",
            "schemas/agent-surface-v1.json",
            "schemas/package-extras-v1.json",
            "schemas/evidence-record-v1.schema.json",
            "docs/CONTRACT_AND_SECURITY_DECISIONS.md",
            "docs/CONTRACT_AND_SECURITY_EVIDENCE.md",
            "docs/MEMORY_API.md",
            "docs/MEMORY_SECURITY.md",
            "docs/PACKAGING.md",
            "docs/A2A_COMPATIBILITY.md",
            "docs/ext/shared-memory/v1.md",
            "skills/operate-a2a-superhub/SKILL.md",
        )
        missing = [relative for relative in required if not (ROOT / relative).is_file()]
        self.assertEqual([], missing, f"missing contract/security artifacts: {missing}")

    def test_public_decision_register_names_behavior_and_fallbacks(self) -> None:
        register = (ROOT / "docs" / "CONTRACT_AND_SECURITY_DECISIONS.md").read_text(encoding="utf-8")
        boundaries = (
            "Protocol binding", "Principal identity", "Truth ownership",
            "Operational durability", "Task-log sedimentation",
            "Supersede authority", "Consumer cursor", "Wakeup safety",
            "Embedding selection", "Skill compatibility",
            "Multi-consumer behavior", "Private backup", "Federation",
        )
        for boundary in boundaries:
            self.assertIn(f"| {boundary} |", register)
        self.assertIn("Ratification record: **APPROVED**", register)
        self.assertNotIn("DEC-", register)
        self.assertNotRegex(register, r"\bM[0-6][A-C]?\b")


if __name__ == "__main__":
    unittest.main()
