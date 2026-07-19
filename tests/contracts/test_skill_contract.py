from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "operate-a2a-superhub"


def contract_fingerprint(root: Path, relative_files: list[str], override: dict | None = None) -> str:
    digest = hashlib.sha256()
    override = override or {}
    for relative in sorted(relative_files):
        value = override.get(relative)
        if value is None:
            value = json.loads((root / relative).read_text(encoding="utf-8"))
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(canonical)
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


class SkillContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compatibility = json.loads((SKILL / "references" / "compatibility.json").read_text(encoding="utf-8"))

    def test_frontmatter_layout_and_generated_ui_metadata(self) -> None:
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        frontmatter = text.split("---\n", 2)[1]
        keys = {line.split(":", 1)[0] for line in frontmatter.splitlines() if ":" in line}
        self.assertEqual({"name", "description"}, keys)
        self.assertIn("A2A Superhub", frontmatter)
        self.assertLess(len(text.splitlines()), 500)
        for reference in ("workflows.md", "security-and-approval.md", "capabilities-and-versions.md", "troubleshooting.md", "compatibility.json"):
            self.assertTrue((SKILL / "references" / reference).is_file(), reference)
            self.assertIn(f"references/{reference}", text)
        ui = (SKILL / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn("$operate-a2a-superhub", ui)
        self.assertNotIn("TODO", text + ui)

    def test_trigger_corpus_has_positive_negative_and_safe_ambiguous_policy(self) -> None:
        corpus = json.loads((ROOT / "tests" / "contracts" / "fixtures" / "skill" / "triggers.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(corpus["positive"]), 5)
        self.assertGreaterEqual(len(corpus["negative"]), 5)
        self.assertGreaterEqual(len(corpus["ambiguous"]), 1)
        self.assertEqual("read-only-discovery", corpus["ambiguousPolicy"])
        self.assertTrue(all("superhub" in prompt.casefold() or "a2a-superhub" in prompt.casefold() for prompt in corpus["positive"]))
        def explicit_product_trigger(prompt: str) -> bool:
            normalized = prompt.casefold()
            return "a2a superhub" in normalized or "a2a-superhub" in normalized or "superhub" in normalized

        positive_rate = sum(explicit_product_trigger(prompt) for prompt in corpus["positive"]) / len(corpus["positive"])
        negative_rejection_rate = sum(not explicit_product_trigger(prompt) for prompt in corpus["negative"]) / len(corpus["negative"])
        self.assertGreaterEqual(positive_rate, 0.95)
        self.assertGreaterEqual(negative_rejection_rate, 0.95)

    def test_fingerprint_matches_all_agent_facing_contracts(self) -> None:
        actual = contract_fingerprint(ROOT, self.compatibility["contractFiles"])
        self.assertEqual(self.compatibility["agentSurfaceRevision"], actual)

    def test_deliberate_api_drift_fails_without_skill_update(self) -> None:
        api_path = "schemas/memory-api-v1.schema.json"
        changed = json.loads((ROOT / api_path).read_text(encoding="utf-8"))
        changed = copy.deepcopy(changed)
        changed["$defs"]["createNoteRequest"]["properties"]["newUnannouncedField"] = {"type": "string"}
        drifted = contract_fingerprint(ROOT, self.compatibility["contractFiles"], {api_path: changed})
        self.assertNotEqual(self.compatibility["agentSurfaceRevision"], drifted)

    def test_deliberate_mcp_and_cli_drift_fail_without_skill_update(self) -> None:
        mcp_path = "schemas/mcp-memory-v1.contract.json"
        changed_mcp = copy.deepcopy(json.loads((ROOT / mcp_path).read_text(encoding="utf-8")))
        changed_mcp["tools"][0]["name"] = "unannounced_tool_name"
        self.assertNotEqual(
            self.compatibility["agentSurfaceRevision"],
            contract_fingerprint(ROOT, self.compatibility["contractFiles"], {mcp_path: changed_mcp}),
        )

        surface_path = "schemas/agent-surface-v1.json"
        changed_surface = copy.deepcopy(json.loads((ROOT / surface_path).read_text(encoding="utf-8")))
        changed_surface["cli"]["implemented"].append("unannounced-command")
        self.assertNotEqual(
            self.compatibility["agentSurfaceRevision"],
            contract_fingerprint(ROOT, self.compatibility["contractFiles"], {surface_path: changed_surface}),
        )


if __name__ == "__main__":
    unittest.main()
