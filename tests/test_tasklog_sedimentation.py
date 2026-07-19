from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from a2a_superhub.auth import Principal
from a2a_superhub.memory import MemoryService
from a2a_superhub.store import HubStore


ADMIN = Principal("local.operator", "operator", "tok_admin", frozenset({"memory.read", "memory.write", "memory.share", "memory.admin"}))


class TaskLogSedimentationTests(unittest.TestCase):
    def _task(self, store: HubStore, suffix: str, state: str, *, intent: str = "memory.sediment", payload=None) -> None:
        task, _ = store.create_task(
            {
                "taskId": f"task_{suffix}", "fromAgent": "agent.alpha", "toAgent": "agent.beta",
                "intent": intent, "payload": payload or {"summary": suffix},
            }
        )
        store.append_event(task["taskId"], f"task.{state}", state=state)

    def test_completed_failed_canceled_sediment_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            hub = HubStore(state)
            for state_name in ("completed", "failed", "canceled"):
                self._task(hub, state_name, state_name)
            memory = MemoryService(state, enable_task_log=True, task_log_intents={"memory.sediment"})

            first = memory.replay_terminal_outbox(hub)
            second = memory.replay_terminal_outbox(hub)

            self.assertEqual({"created": 3, "suppressed": 0, "pending": 0}, first)
            self.assertEqual({"created": 0, "suppressed": 0, "pending": 0}, second)
            notes = memory.timeline(ADMIN, include_superseded=True)
            task_logs = [note for note in notes if note["type"] == "task-log"]
            self.assertEqual(3, len(task_logs))
            serialized = "\n".join(note["body"] for note in task_logs)
            self.assertNotIn('"payload"', serialized)
            self.assertNotIn('"summary"', serialized)
            self.assertIn('"eventSequence"', serialized)
            self.assertEqual([], hub.list_terminal_outbox())

    def test_default_deny_disabled_oversize_and_secret_marked_are_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            hub = HubStore(state)
            self._task(hub, "disabled", "completed", intent="not.allowed")
            self._task(hub, "oversize", "completed", payload={"summary": "x" * 1024})
            self._task(hub, "secret", "completed", payload={"token": "do-not-store"})
            memory = MemoryService(state, enable_task_log=True, task_log_intents={"memory.sediment"})

            result = memory.replay_terminal_outbox(hub, max_payload_bytes=128)

            self.assertEqual(3, result["suppressed"])
            self.assertEqual([], memory.timeline(ADMIN, include_superseded=True))
            serialized = str(memory.list_receipts())
            self.assertNotIn("do-not-store", serialized)

    def test_feature_off_leaves_outbox_pending_then_reenable_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            hub = HubStore(state)
            self._task(hub, "resume", "completed")
            disabled = MemoryService(state)
            self.assertEqual(1, disabled.replay_terminal_outbox(hub)["pending"])
            self.assertEqual(1, len(hub.list_terminal_outbox()))
            enabled = MemoryService(state, enable_task_log=True, task_log_intents={"memory.sediment"})
            self.assertEqual(1, enabled.replay_terminal_outbox(hub)["created"])
            self.assertEqual([], hub.list_terminal_outbox())

    def test_terminal_task_cannot_resurrect_for_second_sedimentation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub = HubStore(Path(tmp))
            self._task(hub, "immutable", "completed")
            with self.assertRaisesRegex(ValueError, "terminal"):
                hub.append_event("task_immutable", "task.working", state="working")


if __name__ == "__main__":
    unittest.main()
