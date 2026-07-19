from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def test_cli_init_and_task_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            subprocess.run([sys.executable, "-m", "a2a_superhub", "--state", str(state), "init"], check=True, text=True, capture_output=True)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "a2a_superhub",
                    "--state",
                    str(state),
                    "task",
                    "create",
                    "--from-agent",
                    "agent.alpha",
                    "--to-agent",
                    "agent.beta",
                    "--summary",
                    "hello",
                    "--idempotency-key",
                    "cli-demo",
                ],
                check=True,
                text=True,
                capture_output=True,
            )
            payload = json.loads(result.stdout)
            self.assertTrue(payload["inserted"])
            self.assertEqual(payload["task"]["idempotencyKey"], "cli-demo")

    def test_memory_cli_derives_local_operator_and_rejects_body_author(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = root / "note.json"
            request.write_text(json.dumps({"type": "note", "title": "CLI", "visibility": "private", "body": "hello"}), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "a2a_superhub", "--state", str(root / "state"), "memory", "note", "create", "--file", str(request)],
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertEqual("local.operator", json.loads(result.stdout)["note"]["author"])
            request.write_text(json.dumps({"type": "note", "title": "spoof", "visibility": "private", "author": "agent.alpha", "body": "hello"}), encoding="utf-8")
            rejected = subprocess.run(
                [sys.executable, "-m", "a2a_superhub", "--state", str(root / "state"), "memory", "note", "create", "--file", str(request)],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(0, rejected.returncode)
            self.assertNotIn("tok_", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
