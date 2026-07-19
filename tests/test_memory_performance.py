import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from a2a_superhub.auth import Principal
from a2a_superhub.memory import MemoryService, note_path


class CountingMemoryService(MemoryService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.delivery_backfills = 0

    def _generate_all_deliveries(self, *args, **kwargs):
        self.delivery_backfills += 1
        return super()._generate_all_deliveries(*args, **kwargs)


class ConnectionCountingMemoryService(MemoryService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.connections = 0

    @contextmanager
    def _connect(self, path):
        self.connections += 1
        with super()._connect(path) as conn:
            yield conn


class MemoryBatchingTests(unittest.TestCase):
    def test_repeated_init_and_calls_do_not_repeat_delivery_backfill(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = CountingMemoryService(Path(tmp), enable_delivery=True)
            service.init()
            service.init()
            service.init()
            self.assertEqual(service.delivery_backfills, 1)

            restarted = CountingMemoryService(Path(tmp), enable_delivery=True)
            restarted.init()
            self.assertEqual(restarted.delivery_backfills, 0)

    def test_disabled_and_external_note_is_backfilled_on_later_enable(self):
        principal = Principal(
            "agent.alpha", "agent", "tok_alpha",
            frozenset({"memory.read", "memory.write", "memory.share"}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            MemoryService(tmp, enable_delivery=True).init()
            writer = MemoryService(tmp, enable_delivery=False, new_note_id=lambda: "mem_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee")
            result = writer.create_note(
                {
                    "type": "handoff", "title": "Offline filesystem note", "visibility": "direct:agent.beta",
                    "about": ["agent.beta"], "body": "Created while delivery was disabled.",
                },
                principal,
                idempotency_key="external-backfill",
            )
            canonical = note_path(writer.root, result.note["id"])
            external = writer.root / "notes" / "human" / "offline-handoff.md"
            external.parent.mkdir(parents=True)
            canonical.replace(external)
            restarted = MemoryService(tmp, enable_delivery=True)
            restarted.init()
            deliveries = restarted.list_deliveries()
            self.assertEqual(3, len(deliveries))
            self.assertEqual({result.note["id"]}, {item["noteId"] for item in deliveries})

    def test_startup_recovery_and_delivery_backfill_use_bounded_connections(self):
        principal = Principal(
            "agent.alpha", "agent", "tok_alpha",
            frozenset({"memory.read", "memory.write", "memory.share"}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            writer = MemoryService(tmp, enable_delivery=False)
            for index in range(100):
                writer.create_note(
                    {
                        "type": "observation", "title": f"Batch {index}", "visibility": "direct:agent.beta",
                        "about": ["agent.beta"], "body": "batch startup fixture",
                    },
                    principal,
                    idempotency_key=f"batch-{index}",
                )
            restarted = ConnectionCountingMemoryService(tmp, enable_delivery=True)
            restarted.init()
            self.assertLessEqual(restarted.connections, 10)
            self.assertEqual(len(restarted.list_deliveries()), 200)


if __name__ == "__main__":
    unittest.main()
