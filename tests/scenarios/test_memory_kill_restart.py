from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from a2a_superhub.auth import Principal
from a2a_superhub.memory import MemoryService, atomic_write, note_path, serialize_note
from a2a_superhub.store import HubStore


OWNER = Principal("agent.alpha", "agent", "tok_owner", frozenset({"memory.read", "memory.write", "memory.share"}))


class MemoryKillRestartScenarios(unittest.TestCase):
    worker = Path(__file__).with_name("memory_kill_worker.py")

    def _kill_at(self, mode: str, state: Path) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")
        process = subprocess.Popen(
            [sys.executable, str(self.worker), mode, str(state)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        line = process.stdout.readline().strip()
        if not line.startswith("READY:"):
            stderr = process.stderr.read()
            process.kill()
            process.communicate(timeout=10)
            self.fail(f"worker did not reach failpoint: {line!r} {stderr!r}")
        process.kill()
        process.communicate(timeout=10)

    def _memory_service(self, state: Path) -> MemoryService:
        return MemoryService(
            state,
            now=lambda: "2026-07-19T12:00:00Z",
            new_note_id=lambda: "mem_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )

    def test_process_kill_across_all_note_write_windows(self) -> None:
        for stage in ("before_replace", "after_replace_before_job", "after_job_before_response"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp)
                self._kill_at(f"memory:{stage}", state)
                service = self._memory_service(state)
                note_id = "mem_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                if stage == "before_replace":
                    self.assertFalse(note_path(service.root, note_id).exists())
                service.init()
                service.process_jobs()
                replay = service.create_note(
                    {"type": "note", "title": "kill scenario", "visibility": "private", "body": "durable after restart"},
                    OWNER,
                    idempotency_key="kill-idem",
                )
                self.assertEqual(note_id, replay.note["id"])
                self.assertEqual(1, len(service.search("durable after restart", OWNER)))

    def test_terminal_outbox_is_atomic_and_replayable_across_process_kill(self) -> None:
        for stage in ("after_event_before_outbox", "after_commit_before_return"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp)
                store = HubStore(state)
                store.create_task({"taskId": "task_kill", "fromAgent": "a", "toAgent": "b"})
                self._kill_at(f"event:{stage}", state)
                reopened = HubStore(state)
                if stage == "after_event_before_outbox":
                    self.assertEqual("submitted", reopened.get_task("task_kill")["state"])
                    self.assertEqual([], reopened.list_terminal_outbox())
                    reopened.append_event("task_kill", "task.completed", state="completed")
                self.assertEqual("completed", reopened.get_task("task_kill")["state"])
                pending = reopened.list_terminal_outbox()
                self.assertEqual(1, len(pending))
                operation_id = pending[0]["operationId"]
                self.assertTrue(reopened.acknowledge_terminal_outbox(operation_id))
                self.assertTrue(reopened.acknowledge_terminal_outbox(operation_id))
                self.assertEqual(1, len(reopened.list_terminal_outbox(pending_only=False)))

    def test_generation_rebuild_kill_keeps_old_index_serving(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            service = self._memory_service(state)
            created = service.create_note(
                {"type": "note", "title": "old generation", "visibility": "private", "body": "old searchable"},
                OWNER,
                idempotency_key="old",
            )
            old_revision = service.source_revision(created.note["id"])

            self._kill_at("index-rebuild:during_index_generation", state)

            reopened = self._memory_service(state)
            self.assertEqual(1, len(reopened.search("old searchable", OWNER)))
            self.assertEqual(old_revision, reopened.source_revision(created.note["id"]))
            self.assertEqual(1, reopened.rebuild_index())
            self.assertEqual(1, len(reopened.search("old searchable", OWNER)))

    def test_index_upsert_revision_transaction_kill_and_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            service = self._memory_service(state)
            created = service.create_note(
                {"type": "note", "title": "revision", "visibility": "private", "body": "before"},
                OWNER,
                idempotency_key="revision",
            )
            note = dict(created.note)
            note["body"] = "after"
            atomic_write(note_path(service.root, note["id"]), serialize_note(note))
            service.recover_jobs()

            self._kill_at("index-upsert:after_index_upsert_before_manifest", state)

            reopened = self._memory_service(state)
            rolled_back = reopened.note_consistency(note["id"])
            self.assertEqual(2, rolled_back["sourceRevision"])
            self.assertEqual(1, rolled_back["indexedRevision"])
            reopened.init()
            reopened.process_jobs()
            self.assertEqual(2, reopened.source_revision(note["id"]))
            self.assertEqual(1, len(reopened.search("after", OWNER)))
            self.assertEqual([], reopened.search("before", OWNER))


if __name__ == "__main__":
    unittest.main()
