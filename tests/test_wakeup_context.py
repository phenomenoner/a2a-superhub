from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from a2a_superhub.auth import Principal
from a2a_superhub.memory import MemoryService, atomic_write, note_path, serialize_note
from a2a_superhub.models import json_dumps
from a2a_superhub.store import HubStore


OWNER = Principal("agent.alpha", "agent", "tok_owner", frozenset({"memory.read", "memory.write", "memory.share"}))
BETA = Principal("agent.beta", "agent", "tok_beta", frozenset({"memory.read"}))


class WakeupContextTests(unittest.TestCase):
    def _service(self, root: Path) -> MemoryService:
        values = iter(f"mem_{index:032x}" for index in range(300, 400))
        return MemoryService(root, new_note_id=lambda: next(values), enable_delivery=True)

    def test_injection_remains_delimited_untrusted_data_and_does_not_ack(self) -> None:
        fixture = json.loads((Path(__file__).parent / "scenarios" / "fixtures" / "wakeup-injection.json").read_text(encoding="utf-8"))
        text = fixture["operations"][0]["text"]
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            service.create_note(
                {"type": "observation", "title": "injection", "visibility": "shared", "about": ["agent.beta"], "body": text},
                OWNER,
                idempotency_key="injection",
            )

            envelope = service.wakeup(BETA, "desktop.a")

            self.assertEqual("data", envelope["role"])
            self.assertEqual("untrusted-memory", envelope["trust"])
            self.assertEqual("data", envelope["items"][0]["role"])
            self.assertEqual("untrusted-memory", envelope["items"][0]["trust"])
            self.assertEqual(text, envelope["items"][0]["note"]["body"])
            self.assertIn("BEGIN UNTRUSTED MEMORY", envelope["items"][0]["delimiter"])
            self.assertNotIn("system", json.dumps(envelope).lower())
            self.assertNotIn("developer", json.dumps(envelope).lower())
            self.assertEqual(1, len(service.fetch_inbox(BETA, "desktop.a")["items"]))

    def test_utf8_budget_never_splits_an_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            service.create_note(
                {"type": "observation", "title": "UTF-8", "visibility": "shared", "about": ["agent.beta"], "body": "甲乙丙" * 200},
                OWNER,
                idempotency_key="utf8",
            )
            full = service.wakeup(BETA, "desktop.a")
            full_size = len(json_dumps(full).encode("utf-8"))
            exact = service.wakeup(BETA, "desktop.a", budget_bytes=full_size)
            below = service.wakeup(BETA, "desktop.a", budget_bytes=full_size - 1)
            self.assertEqual(1, len(exact["items"]))
            self.assertEqual([], below["items"])
            self.assertTrue(below["truncated"])
            self.assertLessEqual(len(json_dumps(below).encode("utf-8")), full_size - 1)

    def test_private_poison_item_is_omitted_from_mixed_wakeup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            service.create_note(
                {"type": "observation", "title": "private", "visibility": "private", "about": ["agent.beta"], "body": "private body"},
                OWNER,
                idempotency_key="private",
            )
            service.create_note(
                {"type": "observation", "title": "shared", "visibility": "shared", "about": ["agent.beta"], "body": "shared body"},
                OWNER,
                idempotency_key="shared",
            )
            envelope = service.wakeup(BETA, "desktop.a")
            serialized = json.dumps(envelope)
            self.assertIn("shared body", serialized)
            self.assertNotIn("private body", serialized)

    def test_fixed_four_sections_mix_auth_filter_and_deterministic_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            hub = HubStore(state)
            hub.create_task({"taskId": "task_active", "fromAgent": "agent.alpha", "toAgent": "agent.beta", "intent": "memory.sediment", "payload": {"secret": "never-pack"}})
            hub.create_task({"taskId": "task_dead", "fromAgent": "agent.alpha", "toAgent": "agent.beta", "intent": "memory.sediment"})
            hub.append_event("task_dead", "task.dead-lettered", state="dead-lettered")
            values = iter(f"mem_{index:032x}" for index in range(400, 500))
            service = MemoryService(state, new_note_id=lambda: next(values), enable_delivery=True, hub_store=hub)
            service.create_note(
                {"type": "profile", "title": "beta profile", "visibility": "shared", "about": ["agent.beta"], "body": "profile visible"},
                OWNER, idempotency_key="profile-visible",
            )
            service.create_note(
                {"type": "profile", "title": "private profile", "visibility": "private", "about": ["agent.beta"], "body": "profile hidden"},
                OWNER, idempotency_key="profile-hidden",
            )
            service.create_note(
                {"type": "observation", "title": "inbox", "visibility": "shared", "about": ["agent.beta"], "body": "inbox visible"},
                OWNER, idempotency_key="inbox-visible",
            )
            service.create_note(
                {"type": "observation", "title": "recent", "visibility": "shared", "participants": ["agent.beta"], "body": "recent visible"},
                OWNER, idempotency_key="recent-visible",
            )

            envelope = service.wakeup(BETA, "desktop.a")
            self.assertEqual(["profile", "inbox", "recent", "activeTasks"], [section["kind"] for section in envelope["sections"]])
            self.assertTrue(all(section["items"] for section in envelope["sections"]))
            serialized = json.dumps(envelope)
            self.assertIn("profile visible", serialized)
            self.assertIn("inbox visible", serialized)
            self.assertIn("recent visible", serialized)
            self.assertIn("task_active", serialized)
            self.assertNotIn("task_dead", serialized)
            self.assertNotIn("profile hidden", serialized)
            self.assertNotIn("never-pack", serialized)
            self.assertEqual(2, len(service.fetch_inbox(BETA, "desktop.a")["items"]))

    def test_recency_is_global_beyond_timeline_page_and_private_latest_does_not_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            service = MemoryService(state, enable_delivery=True)
            for index in range(105):
                note_id = f"mem_{index + 1000:032x}"
                note_type = "profile" if index in {0, 103, 104} else "observation"
                note = {
                    "schema": "a2a-superhub.memory.note.v1", "id": note_id, "type": note_type,
                    "title": f"item {index}", "author": "agent.alpha",
                    "visibility": "private" if index == 104 else "shared",
                    "recordedAt": f"2026-07-19T14:{index // 60:02d}:{index % 60:02d}Z",
                    "source": {"kind": "filesystem"}, "body": f"body {index}",
                }
                if note_type == "profile":
                    note["about"] = ["agent.beta"]
                else:
                    note["participants"] = ["agent.beta"]
                atomic_write(note_path(service.root, note_id), serialize_note(note))
            service.rebuild_index()
            service.init()

            envelope = service.wakeup(BETA, "desktop.a")
            sections = {section["kind"]: section["items"] for section in envelope["sections"]}
            self.assertEqual("item 103", sections["profile"][0]["note"]["title"])
            self.assertEqual("item 102", sections["recent"][0]["note"]["title"])
            serialized = json.dumps(envelope)
            self.assertNotIn("item 104", serialized)
            self.assertNotIn(f"mem_{1104:032x}", serialized)


if __name__ == "__main__":
    unittest.main()
