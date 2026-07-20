from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_gate", ROOT / "tools" / "release_gate.py")
assert SPEC and SPEC.loader
release_gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_gate)


class ReleaseGateIsolationTests(unittest.TestCase):
    def test_subprocesses_ignore_ambient_python_import_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "environment.py"
            script.write_text(
                "import json, os; print(json.dumps({key: os.environ.get(key) for key in "
                "('PYTHONHOME', 'PYTHONPATH', 'PYTHONNOUSERSITE')}))\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"PYTHONHOME": "untrusted-home", "PYTHONPATH": "untrusted-source"},
            ):
                completed = release_gate.run(
                    [sys.executable, str(script)], cwd=Path(temporary), capture=True,
                )
            child = json.loads(completed.stdout)
            self.assertIsNone(child["PYTHONHOME"])
            self.assertIsNone(child["PYTHONPATH"])
            self.assertEqual("1", child["PYTHONNOUSERSITE"])


if __name__ == "__main__":
    unittest.main()
