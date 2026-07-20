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

    def test_operations_cli_backup_restore_and_payload_free_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            subprocess.run(
                [sys.executable, "-m", "a2a_superhub", "--state", str(state), "init"],
                check=True, text=True, capture_output=True,
            )
            subprocess.run(
                [
                    sys.executable, "-m", "a2a_superhub", "--state", str(state),
                    "task", "create", "--from-agent", "agent.alpha", "--to-agent", "agent.beta",
                    "--summary", "do not expose this payload", "--idempotency-key", "ops-cli",
                ],
                check=True, text=True, capture_output=True,
            )
            archive = root / "backup.zip"
            created = subprocess.run(
                [
                    sys.executable, "-m", "a2a_superhub", "--state", str(state),
                    "operations", "backup", "create", "--destination", str(archive),
                ],
                check=True, text=True, capture_output=True,
            )
            self.assertEqual("a2a-superhub.backup.v1", json.loads(created.stdout)["schema"])
            restored = root / "restored"
            result = subprocess.run(
                [
                    sys.executable, "-m", "a2a_superhub", "--state", str(state),
                    "operations", "backup", "restore", "--archive", str(archive),
                    "--target-state", str(restored),
                ],
                check=True, text=True, capture_output=True,
            )
            self.assertEqual("verified", json.loads(result.stdout)["integrity"])
            diagnostic = subprocess.run(
                [
                    sys.executable, "-m", "a2a_superhub", "--state", str(restored),
                    "operations", "diagnostics",
                ],
                check=True, text=True, capture_output=True,
            )
            payload = json.loads(diagnostic.stdout)
            self.assertEqual(1, payload["stores"]["tasks"]["records"])
            self.assertNotIn("do not expose this payload", diagnostic.stdout)
            self.assertNotIn(str(root), diagnostic.stdout)


if __name__ == "__main__":
    unittest.main()
