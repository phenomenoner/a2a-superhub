from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from a2a_superhub.auth import Principal
from a2a_superhub.memory import MemoryService, parse_note
from a2a_superhub.store import HubStore


OWNER = Principal("agent.alpha", "agent", "tok_owner", frozenset({"memory.read", "memory.write", "memory.share"}))
BETA = Principal("agent.beta", "agent", "tok_beta", frozenset({"memory.read"}))
ADMIN = Principal("local.operator", "operator", "tok_admin", frozenset({"memory.read", "memory.write", "memory.share", "memory.admin"}))


class InboxKillRestartScenarios(unittest.TestCase):
    worker = Path(__file__).with_name("memory_kill_worker.py")

    def _kill_at(self, mode: str, state: Path, *args: str) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
        process = subprocess.Popen(
            [sys.executable, str(self.worker), mode, str(state), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        line = process.stdout.readline().strip()
        if not line.startswith("READY:"):
            stderr = process.stderr.read()
            process.kill()
            process.communicate(timeout=10)
            self.fail(f"worker did not reach failpoint: {line!r} {stderr!r}")
        process.kill()
        process.communicate(timeout=10)

    def test_delivery_commit_response_kill_reconciles_one_delivery_and_receipt_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self._kill_at("delivery:after_delivery_before_response", state)
            service = MemoryService(state, enable_delivery=True)
            replay = service.create_note(
                {"type": "handoff", "title": "delivery kill", "visibility": "direct:agent.beta", "body": "deliver after restart"},
                OWNER, idempotency_key="delivery-kill",
            )
            self.assertFalse(replay.inserted)
            self.assertEqual(2, len(service.list_deliveries()))  # direct + handoff reasons, stable logical deliveries
            phases = {item["phase"] for item in service.list_receipts(trace_id=replay.trace_id)}
            self.assertEqual({"write", "index", "delivery"}, phases)

    def test_fetch_and_ack_commit_response_kills_preserve_delivery_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            service = MemoryService(state, enable_delivery=True)
            created = service.create_note(
                {"type": "observation", "title": "fetch kill", "visibility": "shared", "about": ["agent.beta"], "body": "still unread"},
                OWNER, idempotency_key="fetch-kill",
            )
            self._kill_at("fetch:after_fetch_before_response", state)
            reopened = MemoryService(state, enable_delivery=True)
            fetched = reopened.fetch_inbox(BETA, "desktop.a")
            self.assertEqual([created.note["id"]], [item["note"]["id"] for item in fetched["items"]])

            self._kill_at("ack:after_ack_commit_before_response", state, fetched["cursor"])
            after = MemoryService(state, enable_delivery=True)
            self.assertEqual([], after.fetch_inbox(BETA, "desktop.a")["items"])
            retried = after.acknowledge_inbox(BETA, "desktop.a", fetched["cursor"])
            self.assertFalse(retried["acked"])
            self.assertIn("ack", {item["phase"] for item in after.list_receipts(trace_id=created.trace_id)})

    def test_terminal_outbox_to_tasklog_kill_is_exactly_once(self) -> None:
        for stage in ("before_tasklog_write", "after_tasklog_write_before_ack"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp)
                hub = HubStore(state)
                hub.create_task({
                    "taskId": "task_log_kill", "fromAgent": "agent.alpha", "toAgent": "agent.beta",
                    "intent": "memory.sediment", "payload": {"summary": "never persisted"},
                })
                hub.append_event("task_log_kill", "task.completed", state="completed")
                self._kill_at(f"tasklog:{stage}", state)
                memory = MemoryService(state, enable_task_log=True, task_log_intents={"memory.sediment"})
                memory.replay_terminal_outbox(HubStore(state))
                task_logs = [item for item in memory.timeline(ADMIN, include_superseded=True) if item["type"] == "task-log"]
                self.assertEqual(1, len(task_logs))
                self.assertNotIn("never persisted", task_logs[0]["body"])
                self.assertEqual([], HubStore(state).list_terminal_outbox())

    def test_missing_id_rewrite_kill_before_and_after_replace_recovers_stably(self) -> None:
        for stage in ("before_missing_id_rewrite", "after_missing_id_rewrite"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp)
                metadata = {
                    "schema": "a2a-superhub.memory.note.v1", "type": "note", "title": "human",
                    "author": "local.operator", "visibility": "private", "recordedAt": "2026-07-19T13:00:00Z",
                    "source": {"kind": "filesystem"},
                }
                path = state / "memory" / "notes" / "human.md"
                path.parent.mkdir(parents=True)
                path.write_text(f"---\n{yaml.safe_dump(metadata, sort_keys=True)}---\nhuman durable", encoding="utf-8")
                self._kill_at(f"missing-id:{stage}", state)
                before_restart = path.read_bytes()
                if stage == "before_missing_id_rewrite":
                    self.assertNotIn(b"\nid:", before_restart)
                service = MemoryService(state, enable_watcher_side_effects=True)
                service.sync_filesystem()
                assigned = parse_note(path.read_bytes())["id"]
                service.sync_filesystem()
                self.assertEqual(assigned, parse_note(path.read_bytes())["id"])
                self.assertEqual(1, len(service.search("human durable", ADMIN)))


if __name__ == "__main__":
    unittest.main()
