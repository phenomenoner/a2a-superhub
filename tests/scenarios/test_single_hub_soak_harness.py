from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from a2a_superhub.client import HubClient, HubClientError
from a2a_superhub.server import make_server
from tools.single_hub_soak import Runtime, Soak, SoakInvariantError, free_port


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipIf(importlib.util.find_spec("pypdf") is None, "install the derive extra for the soak harness")
class SingleHubSoakHarnessScenarios(unittest.TestCase):
    def test_failure_code_retains_only_a_bounded_server_trace_id(self) -> None:
        valid = HubClientError(
            "fixed public message",
            kind="http",
            status=500,
            code="INTERNAL_ERROR",
            trace_id="trace_" + ("a" * 32),
        )
        malformed = HubClientError(
            "fixed public message",
            kind="http",
            status=500,
            code="INTERNAL_ERROR",
            trace_id="trace_unbounded-private-marker",
        )

        self.assertEqual(
            f"HubClientError:http:500:INTERNAL_ERROR:trace_{'a' * 32}",
            Soak.failure_code(valid),
        )
        self.assertEqual(
            "HubClientError:http:500:INTERNAL_ERROR",
            Soak.failure_code(malformed),
        )

    def test_restart_deadline_is_measured_after_replacement_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            soak = Soak(argparse.Namespace(
                workspace=str(root / "workspace"), evidence=str(root / "evidence.json"),
                duration_seconds=2.0, operation_interval=0.4, artifact_interval=1.0,
                restart_interval=18.0, sample_interval=0.5,
                max_rss_bytes=536_870_912, max_rss_growth_bytes=134_217_728,
                max_state_bytes=2_147_483_648, max_pending_outbox=0,
            ))
            clock = [100.0]

            def slow_restart(*, hard: bool) -> None:
                self.assertFalse(hard)
                clock[0] = 125.0

            soak.restart = slow_restart
            with patch("tools.single_hub_soak.time.monotonic", side_effect=lambda: clock[0]):
                next_restart = soak.restart_and_schedule(hard=False)

            self.assertEqual(143.0, next_restart)

    def test_repeated_cached_diagnostics_are_classified_as_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            soak = Soak(argparse.Namespace(
                workspace=str(root / "workspace"), evidence=str(root / "evidence.json"),
                duration_seconds=2.0, operation_interval=0.4, artifact_interval=1.0,
                restart_interval=10.0, sample_interval=0.5,
                max_rss_bytes=536_870_912, max_rss_growth_bytes=134_217_728,
                max_state_bytes=2_147_483_648, max_pending_outbox=0,
            ))
            process = unittest.mock.Mock()
            process.pid = 123
            process.poll.return_value = None
            soak.runtime.process = process
            snapshot = {
                "generatedAt": "2026-07-24T00:00:00Z",
                "resources": {"stateBytes": 1024},
            }
            soak.admin = unittest.mock.Mock()
            soak.admin.request.return_value = snapshot

            with patch("tools.single_hub_soak.rss_bytes", return_value=2048):
                soak.sample()
                soak.sample()
                soak.sample()
                with self.assertRaisesRegex(SoakInvariantError, "diagnostics-stale"):
                    soak.sample()

    def test_connection_deadline_reports_the_timed_out_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            soak = Soak(argparse.Namespace(
                workspace=str(root / "workspace"), evidence=str(root / "evidence.json"),
                duration_seconds=2.0, operation_interval=0.4, artifact_interval=1.0,
                restart_interval=10.0, sample_interval=0.5,
                max_rss_bytes=536_870_912, max_rss_growth_bytes=134_217_728,
                max_state_bytes=2_147_483_648, max_pending_outbox=0,
            ))

            def disconnected() -> None:
                raise HubClientError("simulated search timeout", kind="connection")

            with (
                patch("tools.single_hub_soak.time.monotonic", side_effect=[0.0, 0.0, 31.0]),
                patch("tools.single_hub_soak.time.sleep"),
            ):
                with self.assertRaisesRegex(SoakInvariantError, "operation-timeout:search"):
                    soak.retry(disconnected, label="search")

    def test_connection_retry_survives_a_measured_expected_recovery_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            soak = Soak(argparse.Namespace(
                workspace=str(root / "workspace"), evidence=str(root / "evidence.json"),
                duration_seconds=2.0, operation_interval=0.4, artifact_interval=1.0,
                restart_interval=10.0, sample_interval=0.5,
                max_rss_bytes=536_870_912, max_rss_growth_bytes=134_217_728,
                max_state_bytes=2_147_483_648, max_pending_outbox=0,
            ))
            soak.runtime._expected_outage = True
            attempts = 0

            def disconnected_once() -> str:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise HubClientError("simulated expected restart", kind="connection")
                return "ready"

            with (
                patch("tools.single_hub_soak.time.monotonic", side_effect=[0.0, 0.0, 31.0, 32.0]),
                patch("tools.single_hub_soak.time.sleep"),
            ):
                self.assertEqual("ready", soak.retry(disconnected_once, label="search"))

    def test_unexpected_child_exit_is_classified_before_connection_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            soak = Soak(argparse.Namespace(
                workspace=str(root / "workspace"), evidence=str(root / "evidence.json"),
                duration_seconds=2.0, operation_interval=0.4, artifact_interval=1.0,
                restart_interval=10.0, sample_interval=0.5,
                max_rss_bytes=536_870_912, max_rss_growth_bytes=134_217_728,
                max_state_bytes=2_147_483_648, max_pending_outbox=0,
            ))
            process = unittest.mock.Mock()
            process.poll.return_value = 17
            soak.runtime.process = process

            def disconnected() -> None:
                raise HubClientError("simulated dead child", kind="connection")

            with patch("tools.single_hub_soak.time.monotonic", side_effect=[0.0, 0.0, 31.0]):
                with self.assertRaisesRegex(SoakInvariantError, "hub-process-exited"):
                    soak.retry(disconnected)

    def test_runtime_preserves_private_child_process_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            principals = root / "principals.json"
            principals.write_text("{}", encoding="utf-8")
            runtime = Runtime(root / "state", principals, free_port())
            try:
                runtime.start()
            finally:
                runtime.stop(hard=False)
            stdout = root / "server.stdout.log"
            stderr = root / "server.stderr.log"
            self.assertTrue(stdout.is_file())
            self.assertTrue(stderr.is_file())
            self.assertIn('"ready": true', stdout.read_text(encoding="utf-8"))

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
            soak.admin = unittest.mock.Mock()
            soak.admin.request.return_value = {
                "stores": {
                    "memory": {"pendingJobs": 99, "activeQuarantine": 99},
                    "tasks": {"pendingTerminalOutbox": 99},
                },
            }
            result = soak.run()
            self.assertFalse(result["passed"])
            self.assertIn("sample:HubClientError:connection:0:none", result["failures"])
            self.assertIn("audit", result)
            self.assertEqual(0, result["audit"]["lostTasks"])
            self.assertEqual(0, result["audit"]["activeQuarantine"])
            self.assertEqual(0, result["audit"]["pendingJobs"])
            self.assertEqual(0, result["audit"]["pendingTerminalOutbox"])
            soak.admin.request.assert_not_called()

    def test_delivery_drain_stays_below_rate_limit_when_delivery_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            soak = Soak(argparse.Namespace(
                workspace=str(root / "workspace"), evidence=str(root / "evidence.json"),
                duration_seconds=2.0, operation_interval=0.4, artifact_interval=1.0,
                restart_interval=10.0, sample_interval=0.5,
                max_rss_bytes=536_870_912, max_rss_growth_bytes=134_217_728,
                max_state_bytes=2_147_483_648, max_pending_outbox=0,
            ))
            principals = {
                "reader-token": {
                    "subject": "agent.beta",
                    "kind": "agent",
                    "tokenId": "tok_reader_test",
                    "scopes": ["memory.read"],
                },
            }
            server = make_server(
                soak.state,
                host="127.0.0.1",
                port=free_port(),
                principals=principals,
                rate_limit=4,
                enable_memory=True,
                enable_delivery=True,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                soak.reader = HubClient(
                    f"http://127.0.0.1:{server.server_port}",
                    token="reader-token",
                    timeout=2,
                )
                soak.delivery_note_ids.add("mem_00000000000000000000000000000000")
                with self.assertRaisesRegex(SoakInvariantError, "delivery-drain-timeout"):
                    soak.drain_deliveries(timeout=3.2)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

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
                    "--duration-seconds", "12",
                    "--operation-interval", "0.4",
                    "--artifact-interval", "1.5",
                    "--restart-interval", "2",
                    "--sample-interval", "0.5",
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                timeout=120,
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
