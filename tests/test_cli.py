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


if __name__ == "__main__":
    unittest.main()
