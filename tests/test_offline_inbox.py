from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from a2a_superhub.auth import Principal
from a2a_superhub.memory import CursorError, MemoryService, atomic_write, note_path, serialize_note


OWNER = Principal("agent.alpha", "agent", "tok_owner", frozenset({"memory.read", "memory.write", "memory.share"}))
BETA = Principal("agent.beta", "agent", "tok_beta", frozenset({"memory.read"}))


class OfflineInboxTests(unittest.TestCase):
    def test_delivery_identity_is_stable_across_retry_watch_and_reindex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(
                Path(tmp),
                now=lambda: "2026-07-19T13:00:00Z",
                new_note_id=lambda: "mem_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                enable_delivery=True,
            )
            created = service.create_note(
                {
                    "type": "handoff",
                    "title": "Handoff to beta and gamma",
                    "visibility": "direct:agent.beta",
                    "about": ["agent.gamma"],
                    "body": "Deterministic delivery",
                },
                OWNER,
                idempotency_key="delivery-stable",
            )

            replay = service.create_note(
                {
                    "type": "handoff",
                    "title": "Handoff to beta and gamma",
                    "visibility": "direct:agent.beta",
                    "about": ["agent.gamma"],
                    "body": "Deterministic delivery",
                },
                OWNER,
                idempotency_key="delivery-stable",
            )
            service.sync_filesystem()
            service.rebuild_index()
            service.generate_deliveries(created.note["id"])

            self.assertFalse(replay.inserted)
            deliveries = service.list_deliveries()
            identities = {(item["noteId"], item["recipient"], item["reason"]) for item in deliveries}
            self.assertEqual(
                {
                    (created.note["id"], "agent.beta", "direct"),
                    (created.note["id"], "agent.beta", "handoff"),
                    (created.note["id"], "agent.gamma", "about"),
                },
                identities,
            )
            self.assertEqual(3, len(deliveries))

    def test_fetch_does_not_ack_and_two_consumers_are_restart_independent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            service = MemoryService(state, enable_delivery=True)
            service.create_note(
                {"type": "observation", "title": "offline", "visibility": "shared", "about": ["agent.beta"], "body": "provenance"},
                OWNER,
                idempotency_key="offline",
            )
            first_a = service.fetch_inbox(BETA, "desktop.a")
            second_a = service.fetch_inbox(BETA, "desktop.a")
            first_b = service.fetch_inbox(BETA, "desktop.b")
            self.assertEqual(1, len(first_a["items"]))
            self.assertEqual(1, len(second_a["items"]))
            self.assertEqual(1, len(first_b["items"]))
            self.assertNotEqual(first_a["cursor"], second_a["cursor"])
            service.rebuild_index()
            self.assertEqual(1, len(service.fetch_inbox(BETA, "desktop.a")["items"]))

            restarted = MemoryService(state, enable_delivery=True)
            acked = restarted.acknowledge_inbox(BETA, "desktop.a", first_a["cursor"])
            self.assertTrue(acked["acked"])
            self.assertEqual([], restarted.fetch_inbox(BETA, "desktop.a")["items"])
            self.assertEqual(1, len(restarted.fetch_inbox(BETA, "desktop.b")["items"]))
            stale = restarted.acknowledge_inbox(BETA, "desktop.a", second_a["cursor"])
            self.assertFalse(stale["acked"])

    def test_forged_bound_and_unissued_cursor_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(Path(tmp), enable_delivery=True, cursor_secret=b"cursor-secret" * 3)
            service.init()
            issued = service.fetch_inbox(BETA, "desktop.a")["cursor"]
            with self.assertRaises(CursorError):
                service.acknowledge_inbox(BETA, "desktop.b", issued)
            forged = issued[:-1] + ("A" if issued[-1] != "A" else "B")
            with self.assertRaises(CursorError):
                service.acknowledge_inbox(BETA, "desktop.a", forged)
            valid_mac_but_unissued = service._encode_cursor("agent.beta", "desktop.a", 0)
            with self.assertRaises(CursorError):
                service.acknowledge_inbox(BETA, "desktop.a", valid_mac_but_unissued)

    def test_unread_and_ack_survive_reindex_across_cursor_secret_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            first = MemoryService(state, enable_delivery=True, cursor_secret=b"secret-a" * 8)
            created = first.create_note(
                {"type": "observation", "title": "rotation", "visibility": "shared", "about": ["agent.beta"], "body": "unread survives"},
                OWNER, idempotency_key="rotation",
            )
            old_page = first.fetch_inbox(BETA, "desktop.a")
            first.rebuild_index()

            rotated = MemoryService(state, enable_delivery=True, cursor_secret=b"secret-b" * 8)
            with self.assertRaises(CursorError):
                rotated.acknowledge_inbox(BETA, "desktop.a", old_page["cursor"])
            new_page = rotated.fetch_inbox(BETA, "desktop.a")
            self.assertEqual([created.note["id"]], [item["note"]["id"] for item in new_page["items"]])
            rotated.acknowledge_inbox(BETA, "desktop.a", new_page["cursor"])
            rotated.rebuild_index()

            restarted = MemoryService(state, enable_delivery=True, cursor_secret=b"secret-b" * 8)
            self.assertEqual([], restarted.fetch_inbox(BETA, "desktop.a")["items"])

    def test_revoked_poison_item_is_hidden_but_does_not_block_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(Path(tmp), enable_delivery=True)
            revoked = service.create_note(
                {"type": "observation", "title": "revoked", "visibility": "direct:agent.beta", "body": "private later"},
                OWNER,
                idempotency_key="revoked",
            )
            service.create_note(
                {"type": "observation", "title": "visible", "visibility": "direct:agent.beta", "body": "still visible"},
                OWNER,
                idempotency_key="visible",
            )
            changed = dict(revoked.note)
            changed["visibility"] = "private"
            atomic_write(note_path(service.root, changed["id"]), serialize_note(changed))

            fetched = service.fetch_inbox(BETA, "desktop.a")

            self.assertEqual(["visible"], [item["note"]["title"] for item in fetched["items"]])
            service.acknowledge_inbox(BETA, "desktop.a", fetched["cursor"])
            self.assertEqual([], service.fetch_inbox(BETA, "desktop.a")["items"])

    def test_delivery_feature_off_preserves_notes_and_resume_backfills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            disabled = MemoryService(state)
            note = disabled.create_note(
                {"type": "observation", "title": "disabled", "visibility": "shared", "about": ["agent.beta"], "body": "resume"},
                OWNER,
                idempotency_key="disabled",
            )
            self.assertEqual([], disabled.list_deliveries())
            enabled = MemoryService(state, enable_delivery=True)
            enabled.init()
            self.assertEqual(1, len(enabled.list_deliveries()))
            self.assertEqual(note.note["id"], enabled.read_note(note.note["id"], OWNER)["id"])


if __name__ == "__main__":
    unittest.main()
