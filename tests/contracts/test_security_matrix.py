from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MATRIX = ROOT / "tests" / "contracts" / "fixtures" / "security" / "access-matrix.json"


class SecurityMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.matrix = json.loads(MATRIX.read_text(encoding="utf-8"))

    def can_read(self, principal: dict, note: dict) -> bool:
        scopes = set(principal["scopes"])
        if "memory.read" not in scopes:
            return False
        if "memory.admin" in scopes or principal["subject"] == note["author"]:
            return True
        if note["visibility"] == "shared":
            return True
        return note["visibility"] == f"direct:{principal['subject']}"

    def test_every_principal_visibility_read_cell_on_every_surface(self) -> None:
        checked = 0
        for surface in self.matrix["readSurfaces"]:
            for principal_name, principal in self.matrix["principals"].items():
                expected_names = set(self.matrix["expectedRead"][principal_name])
                for note_name, note in self.matrix["notes"].items():
                    self.assertEqual(note_name in expected_names, self.can_read(principal, note), (surface, principal_name, note_name))
                    checked += 1
        expected_cells = len(self.matrix["readSurfaces"]) * len(self.matrix["principals"]) * len(self.matrix["notes"])
        self.assertEqual(expected_cells, checked)

    def test_create_scope_visibility_matrix(self) -> None:
        for visibility, expected_names in self.matrix["expectedCreate"].items():
            for principal_name, principal in self.matrix["principals"].items():
                scopes = set(principal["scopes"])
                allowed = "memory.write" in scopes and (visibility == "private" or "memory.share" in scopes)
                self.assertEqual(principal_name in expected_names, allowed, (principal_name, visibility))

    def test_supersede_authority_matrix(self) -> None:
        expected = set(self.matrix["expectedSupersede"])
        for principal_name, principal in self.matrix["principals"].items():
            allowed = "memory.write" in principal["scopes"] and (
                principal["subject"] == self.matrix["notes"]["shared"]["author"] or "memory.admin" in principal["scopes"]
            )
            self.assertEqual(principal_name in expected, allowed, principal_name)


if __name__ == "__main__":
    unittest.main()
