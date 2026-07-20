from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from a2a_superhub.client import HubClientError
from tools.single_hub_soak import Soak


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipIf(importlib.util.find_spec("pypdf") is None, "install the derive extra for the soak harness")
class SingleHubSoakHarnessScenarios(unittest.TestCase):
    def test_sample_failure_preserves_full_audit_and_classified_cause(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            soak = Soak(argparse.Namespace(
                workspace=str(root / "workspace"), evidence=str(root / "evidence.json"),
                duration_seconds=2.0, operation_interval=0.4, artifact_interval=1.0,
                restart_interval=10.0, sample_interval=0.5,
                max_rss_bytes=536_870_912, max_rss_growth_bytes=134_217_728,
                max_state_bytes=2_147_483_648, max_pending_outbox=0,
            ))

            def fail_sample() -> None:
                raise HubClientError("simulated diagnostics timeout", kind="connection")

            soak.sample = fail_sample
            result = soak.run()
            self.assertFalse(result["passed"])
            self.assertIn("sample:HubClientError:connection:0:none", result["failures"])
            self.assertIn("audit", result)
            self.assertEqual(0, result["audit"]["lostTasks"])
            self.assertEqual(0, result["audit"]["activeQuarantine"])

    def test_real_http_load_graceful_restart_controlled_kill_and_final_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            evidence = root / "evidence.json"
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(ROOT / "src")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools" / "single_hub_soak.py"),
                    "--workspace", str(workspace),
                    "--evidence", str(evidence),
                    "--duration-seconds", "7",
                    "--operation-interval", "0.4",
                    "--artifact-interval", "1.5",
                    "--restart-interval", "2",
                    "--sample-interval", "0.5",
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                timeout=90,
            )
            result = json.loads(evidence.read_text(encoding="utf-8"))
            self.assertEqual(0, completed.returncode, (completed.stdout, completed.stderr, result))
            self.assertTrue(result["passed"])
            self.assertGreaterEqual(result["restarts"]["graceful"], 1)
            self.assertGreaterEqual(result["restarts"]["controlledKill"], 1)
            self.assertGreaterEqual(result["workload"]["filesystemWrites"], 1)
            self.assertGreaterEqual(result["workload"]["expectedDeliveries"], 1)
            self.assertEqual(0, result["audit"]["lostTasks"])
            self.assertEqual(0, result["audit"]["lostNotes"])
            self.assertEqual(0, result["audit"]["lostArtifacts"])
            self.assertEqual(0, result["audit"]["lostDeliveries"])
            self.assertEqual(0, result["audit"]["unacknowledgedDeliveries"])
            self.assertEqual(0, result["audit"]["privateAuthorizationLeaks"])
            self.assertNotIn(str(root), evidence.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
