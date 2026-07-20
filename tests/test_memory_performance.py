import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from a2a_superhub.auth import Principal
from a2a_superhub import memory as memory_module
from a2a_superhub import operations as operations_module
from a2a_superhub.memory import MemoryService, atomic_write, note_path, serialize_note
from a2a_superhub.operations import OperationsDiagnostics


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

    def test_index_lag_diagnostics_use_one_manifest_snapshot_not_one_connection_per_note(self):
        principal = Principal(
            "agent.alpha", "agent", "tok_alpha",
            frozenset({"memory.read", "memory.write"}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            writer = MemoryService(tmp)
            for index in range(100):
                writer.create_note(
                    {
                        "type": "observation", "title": f"Index {index}",
                        "visibility": "private", "body": "bounded lag fixture",
                    },
                    principal,
                    idempotency_key=f"index-{index}",
                )
            reader = ConnectionCountingMemoryService(tmp)
            status = reader.index_status(include_lag_records=True)
            self.assertEqual(0, status["lagRecords"])
            self.assertLessEqual(reader.connections, 3)

    def test_index_lag_scan_only_reparses_changed_notes(self):
        principal = Principal(
            "agent.alpha", "agent", "tok_alpha",
            frozenset({"memory.read", "memory.write"}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(tmp)
            for index in range(20):
                service.create_note(
                    {
                        "type": "observation", "title": f"Cached {index}",
                        "visibility": "private", "body": "unchanged scan fixture",
                    },
                    principal,
                    idempotency_key=f"cached-{index}",
                )
            service.index_status(include_lag_records=True)
            original_parse = memory_module.parse_note
            parse_calls = 0

            def counted_parse(data):
                nonlocal parse_calls
                parse_calls += 1
                return original_parse(data)

            with patch.object(memory_module, "parse_note", side_effect=counted_parse):
                self.assertEqual(0, service.index_status(include_lag_records=True)["lagRecords"])
                self.assertEqual(0, parse_calls)
                note = service.read_note(next(iter(service._authoritative_catalog)), principal)
                note["body"] = "changed outside the service"
                atomic_write(note_path(service.root, note["id"]), serialize_note(note))
                self.assertEqual(1, service.index_status(include_lag_records=True)["lagRecords"])
                self.assertEqual(1, parse_calls)

    def test_repeated_diagnostics_reuse_validated_notes_and_one_inventory_pass(self):
        writer = Principal(
            "agent.alpha", "agent", "tok_alpha",
            frozenset({"memory.read", "memory.write"}),
        )
        admin = Principal("local.operator", "operator", "tok_admin", frozenset({"hub.admin"}))
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(tmp)
            for index in range(20):
                service.create_note(
                    {
                        "type": "observation", "title": f"Diagnostic {index}",
                        "visibility": "private", "body": "payload-free inventory fixture",
                    },
                    writer,
                    idempotency_key=f"diagnostic-{index}",
                )
            diagnostics = OperationsDiagnostics(tmp, memory_service=service)
            original_parse = memory_module.parse_note
            parse_calls = 0

            def counted_parse(data):
                nonlocal parse_calls
                parse_calls += 1
                return original_parse(data)

            with patch.object(
                operations_module, "_state_inventory", wraps=operations_module._state_inventory,
            ) as inventory:
                first = diagnostics.collect(admin)
            self.assertEqual(1, inventory.call_count)
            with patch.object(memory_module, "parse_note", side_effect=counted_parse):
                second = diagnostics.collect(admin)
            self.assertEqual(0, parse_calls)
            self.assertEqual(20, first["stores"]["memory"]["records"])
            self.assertEqual(first["stores"]["memory"]["index"], second["stores"]["memory"]["index"])

    def test_diagnostics_reject_a_memory_cache_for_another_state(self):
        admin = Principal("local.operator", "operator", "tok_admin", frozenset({"hub.admin"}))
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            with self.assertRaisesRegex(ValueError, "same state"):
                OperationsDiagnostics(first, memory_service=MemoryService(second)).collect(admin)


if __name__ == "__main__":
    unittest.main()
