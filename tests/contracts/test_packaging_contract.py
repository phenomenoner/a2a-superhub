from __future__ import annotations

import importlib
import importlib.metadata
import ast
import json
import os
import tomllib
import unittest
from pathlib import Path

from a2a_superhub.skill_package import SKILL_PAYLOAD


ROOT = Path(__file__).resolve().parents[2]


class PackagingContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        cls.contract = json.loads((ROOT / "schemas" / "package-extras-v1.json").read_text(encoding="utf-8"))

    def test_core_has_zero_unconditional_runtime_dependencies(self) -> None:
        self.assertEqual([], self.pyproject["project"]["dependencies"])
        self.assertEqual([], self.contract["coreRuntimeDependencies"])
        installed_requirements = importlib.metadata.requires("a2a-superhub") or []
        self.assertTrue(all("extra ==" in requirement for requirement in installed_requirements), installed_requirements)

    def test_pyproject_extras_match_machine_contract_and_umbrella_union(self) -> None:
        declared = self.pyproject["project"]["optional-dependencies"]
        contract_extras = self.contract["extras"]
        for name in ("memory-core", "search", "mcp", "derive", "memory"):
            self.assertIn(name, declared)
            self.assertEqual(contract_extras[name]["dependencies"], declared[name], name)
        union = []
        for name in contract_extras["memory"]["includes"]:
            for dependency in contract_extras[name]["dependencies"]:
                if dependency not in union:
                    union.append(dependency)
        self.assertEqual(contract_extras["memory"]["dependencies"], union)

    def test_search_and_reference_deriver_dependencies_are_pinned(self) -> None:
        search = self.contract["extras"]["search"]
        self.assertTrue(search["runtimeImplemented"])
        self.assertEqual("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", search["embeddingModel"])
        self.assertEqual("faf4aa4225822f3bc6376869cb1164e8e3feedd0", search["embeddingRevision"])
        self.assertEqual("Qdrant/bm25", search["sparseModel"])
        self.assertEqual("apache-2.0", search["embeddingLicense"])
        self.assertEqual(["qdrant-client[fastembed]==1.18.0"], search["dependencies"])
        derive = self.contract["extras"]["derive"]
        self.assertEqual(["pypdf==6.6.1", "Pillow==12.2.0"], derive["dependencies"])
        self.assertTrue(derive["runtimeImplemented"])
        self.assertIn("tesseract", derive["externalProvider"].casefold())

    def test_mcp_extra_has_a_real_runtime_entry_point(self) -> None:
        mcp = self.contract["extras"]["mcp"]
        self.assertTrue(mcp["runtimeImplemented"])
        self.assertEqual(["mcp==1.28.1"], mcp["dependencies"])
        scripts = self.pyproject["project"]["scripts"]
        self.assertEqual("a2a_superhub.mcp_server:main", scripts["a2a-superhub-mcp"])

    def test_selected_extra_imports_in_fresh_environment(self) -> None:
        selected = os.environ.get("A2A_TEST_EXTRA")
        if not selected:
            self.skipTest("set A2A_TEST_EXTRA in the fresh-extra installation job")
        imports = {
            "memory-core": ("yaml", "watchdog"),
            "search": ("qdrant_client", "fastembed"),
            "mcp": ("mcp",),
            "derive": ("pypdf", "PIL"),
            "memory": ("yaml", "watchdog", "qdrant_client", "fastembed", "mcp", "pypdf", "PIL"),
        }
        self.assertIn(selected, imports)
        for module in imports[selected]:
            self.assertIsNotNone(importlib.import_module(module), (selected, module))

    def test_build_skill_allowlist_matches_runtime_installer_allowlist(self) -> None:
        tree = ast.parse((ROOT / "setup.py").read_text(encoding="utf-8"))
        assignment = next(
            node for node in tree.body
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "SKILL_FILES" for target in node.targets)
        )
        self.assertEqual(SKILL_PAYLOAD, ast.literal_eval(assignment.value))


if __name__ == "__main__":
    unittest.main()
