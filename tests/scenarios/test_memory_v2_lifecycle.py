from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from a2a_superhub.auth import Principal
from a2a_superhub.memory import (
    CURSOR_PURPOSE_UNKNOWN,
    DELIVERY_MODEL_V1,
    CursorRefreshRequiredError,
    MemoryService,
    atomic_write,
    note_path,
    serialize_note,
)


OWNER = Principal(
    "agent.alpha",
    "agent",
    "tok_owner",
    frozenset({"memory.read", "memory.write", "memory.share"}),
)
RECEIVER = Principal(
    "agent.beta", "agent", "tok_receiver", frozenset({"memory.read"})
)
UNRELATED = Principal(
    "agent.gamma", "agent", "tok_unrelated", frozenset({"memory.read"})
)


class MemoryV2LifecycleScenarios(unittest.TestCase):
    def test_lifecycle_is_authorized_independent_facts_not_a_linear_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = MemoryService(Path(temporary), enable_delivery=True)
            created = service.create_note(
                {
                    "type": "handoff",
                    "title": "Lifecycle source",
                    "visibility": "direct:agent.beta",
                    "body": "facts only",
                },
                OWNER,
                idempotency_key="lifecycle-source",
            )
            page = service.fetch_inbox(RECEIVER, "desktop.one")
            service.acknowledge_inbox(
                RECEIVER, "desktop.one", page["cursor"]
            )
            linked = service.create_note(
                {
                    "type": "observation",
                    "title": "Authorized reference",
                    "visibility": "shared",
                    "relations": [
                        {
                            "type": "references",
                            "target": f"note:{created.note['id']}",
                        }
                    ],
                    "body": "linked fact",
                },
                OWNER,
                idempotency_key="lifecycle-linked",
                contract_version=2,
            )

            author_view = service.note_lifecycle(created.note["id"], OWNER)
            receiver_view = service.note_lifecycle(created.note["id"], RECEIVER)

            self.assertNotIn("state", author_view)
            self.assertNotIn("understood", str(author_view).lower())
            self.assertNotIn(
                "consumerId",
                author_view["facts"]["deliveries"][0],
            )
            self.assertTrue(
                author_view["facts"]["deliveries"][0]["acknowledged"]
            )
            self.assertEqual(
                "desktop.one",
                receiver_view["facts"]["deliveries"][0][
                    "acknowledgements"
                ][0]["consumerId"],
            )
            self.assertEqual(
                linked.note["id"],
                receiver_view["facts"]["linkedReferences"][0]["noteId"],
            )
            with self.assertRaises(KeyError):
                service.note_lifecycle(created.note["id"], UNRELATED)

    def test_first_route_snapshot_includes_an_empty_route_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = MemoryService(Path(temporary), enable_delivery=True)
            created = service.create_note(
                {
                    "type": "observation",
                    "title": "No initial route",
                    "visibility": "shared",
                    "body": "first evaluation is empty",
                },
                OWNER,
                idempotency_key="empty-route",
            )
            changed = dict(created.note)
            changed["about"] = [RECEIVER.subject]
            atomic_write(
                note_path(service.root, changed["id"]),
                serialize_note(changed),
            )

            service.sync_filesystem()

            self.assertEqual([], service.fetch_inbox(RECEIVER, "desktop")["items"])
            with service._connect(service.ops_path) as connection:
                snapshot = connection.execute(
                    """
                    SELECT routes_json, conflict_json
                    FROM delivery_route_snapshots WHERE note_id=?
                    """,
                    (created.note["id"],),
                ).fetchone()
            self.assertEqual("[]", snapshot["routes_json"])
            self.assertIsNotNone(snapshot["conflict_json"])

    def test_unknown_purpose_legacy_cursor_cannot_advance_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = MemoryService(
                Path(temporary),
                enable_delivery=True,
                cursor_secret=b"legacy-purpose-secret" * 2,
            )
            service.init()
            token = service._encode_cursor(
                RECEIVER.subject,
                "desktop",
                7,
                token_version=1,
                delivery_model=DELIVERY_MODEL_V1,
                cursor_kind=CURSOR_PURPOSE_UNKNOWN,
            )
            digest = hashlib.sha256(token.encode("ascii")).hexdigest()
            with service._connect(service.ops_path) as connection:
                connection.execute(
                    """
                    INSERT INTO issued_cursors(
                        cursor_hash, principal, consumer_id, sequence,
                        issued_at, token_version, delivery_model, cursor_kind
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        digest,
                        RECEIVER.subject,
                        "desktop",
                        7,
                        "2026-07-26T00:00:00Z",
                        1,
                        DELIVERY_MODEL_V1,
                        CURSOR_PURPOSE_UNKNOWN,
                    ),
                )

            with self.assertRaises(CursorRefreshRequiredError):
                service.acknowledge_inbox(RECEIVER, "desktop", token)

            with service._connect(service.ops_path) as connection:
                row = connection.execute(
                    """
                    SELECT acked_sequence FROM consumer_cursors
                    WHERE principal=? AND consumer_id=?
                    """,
                    (RECEIVER.subject, "desktop"),
                ).fetchone()
            self.assertIsNone(row)


if __name__ == "__main__":
    unittest.main()
