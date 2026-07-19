from __future__ import annotations

import tempfile
import sqlite3
import threading
import unittest
from pathlib import Path

from a2a_superhub.store import HubStore


class TaskLifecycleEventContractTests(unittest.TestCase):
    def _store_with_task(self, root: Path) -> tuple[HubStore, str]:
        store = HubStore(root)
        task, inserted = store.create_task(
            {
                "taskId": "task_evt_contract",
                "conversationId": "conv_evt_contract",
                "fromAgent": "caller",
                "toAgent": "worker",
            }
        )
        self.assertTrue(inserted)
        return store, task["taskId"]

    def test_event_sequence_is_monotonic_even_when_timestamps_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, task_id = self._store_with_task(Path(tmp))
            store.append_event(task_id, "task.accepted", state="accepted")
            store.append_event(task_id, "task.working", state="working")

            events = store.list_events(task_id)

            self.assertEqual([1, 2, 3], [event["sequence"] for event in events])

    def test_terminal_state_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, task_id = self._store_with_task(Path(tmp))
            store.append_event(task_id, "task.completed", state="completed")

            with self.assertRaisesRegex(ValueError, "terminal"):
                store.append_event(task_id, "task.progress", {"late": True})

    def test_transition_graph_rejects_state_regression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, task_id = self._store_with_task(Path(tmp))
            store.append_event(task_id, "task.accepted", state="accepted")
            store.append_event(task_id, "task.working", state="working")

            with self.assertRaisesRegex(ValueError, "transition"):
                store.append_event(task_id, "task.accepted-again", state="accepted")

    def test_concurrent_writers_all_commit_with_one_contiguous_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, task_id = self._store_with_task(Path(tmp))
            barrier = threading.Barrier(9)
            errors: list[Exception] = []

            def append(index: int) -> None:
                barrier.wait()
                try:
                    store.append_event(task_id, "task.progress", {"writer": index})
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            threads = [threading.Thread(target=append, args=(index,)) for index in range(8)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join()

            self.assertEqual([], errors)
            first = store.list_events(task_id)
            second = store.list_events(task_id)
            self.assertEqual(list(range(1, 10)), [event["sequence"] for event in first])
            self.assertEqual([event["eventId"] for event in first], [event["eventId"] for event in second])

    def test_v1_same_timestamp_events_backfill_stably(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks" / "hub-tasks.sqlite"
            db_path.parent.mkdir(parents=True)
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE tasks(
                        task_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, idempotency_key TEXT UNIQUE,
                        from_agent TEXT NOT NULL, to_agent TEXT NOT NULL, intent TEXT NOT NULL, state TEXT NOT NULL,
                        payload_json TEXT NOT NULL, artifact_refs_json TEXT NOT NULL, permissions_json TEXT NOT NULL,
                        limits_json TEXT NOT NULL, correlation_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                    );
                    CREATE TABLE events(
                        event_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL,
                        state TEXT, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
                    );
                    INSERT INTO tasks VALUES ('legacy', 'conv', NULL, 'a', 'b', 'agent.query', 'submitted', '{}', '[]', '{}', '{}', '{}', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
                    INSERT INTO events VALUES ('evt_b', 'legacy', 'second', NULL, '{}', '2026-01-01T00:00:00Z');
                    INSERT INTO events VALUES ('evt_a', 'legacy', 'first', NULL, '{}', '2026-01-01T00:00:00Z');
                    """
                )
                conn.commit()
            finally:
                conn.close()

            store = HubStore(Path(tmp))
            store.init()
            first = store.list_events("legacy")
            store.init()
            second = store.list_events("legacy")

            self.assertEqual(["evt_b", "evt_a"], [event["eventId"] for event in first])
            self.assertEqual([1, 2], [event["sequence"] for event in first])
            self.assertEqual(first, second)

    def test_terminal_commit_creates_one_durable_replayable_outbox_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, task_id = self._store_with_task(root)
            event = store.append_event(task_id, "task.completed", {"result": "ok"}, state="completed")

            restarted = HubStore(root)
            pending = restarted.list_terminal_outbox()

            self.assertEqual(1, len(pending))
            self.assertEqual(f"tasklog:{task_id}:{event['sequence']}", pending[0]["operationId"])
            self.assertEqual("completed", pending[0]["terminalState"])
            self.assertTrue(restarted.acknowledge_terminal_outbox(pending[0]["operationId"], acknowledged_at="2026-01-01T00:00:00Z"))
            self.assertEqual([], restarted.list_terminal_outbox())
            self.assertEqual(1, len(restarted.list_terminal_outbox(pending_only=False)))


if __name__ == "__main__":
    unittest.main()
