from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from a2a_superhub.auth import Principal
from a2a_superhub.memory import (
    DELIVERY_MODEL_V1,
    MemoryError,
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
    "agent.beta",
    "agent",
    "tok_receiver",
    frozenset({"memory.read"}),
)


class LogicalDeliveryMigrationScenarios(unittest.TestCase):
    def _write_v3_fixture(self, state: Path, acknowledged: int) -> str:
        note_id = "mem_11111111111111111111111111111111"
        note = {
            "schema": "a2a-superhub.memory.note.v1",
            "id": note_id,
            "type": "handoff",
            "title": "Legacy three-reason handoff",
            "author": OWNER.subject,
            "visibility": f"direct:{RECEIVER.subject}",
            "recordedAt": "2026-07-25T00:00:00Z",
            "source": {"kind": "api"},
            "about": [RECEIVER.subject],
            "body": "migration fixture",
        }
        atomic_write(note_path(state / "memory", note_id), serialize_note(note))
        ops = state / "memory" / "ops.sqlite"
        with closing(sqlite3.connect(ops)) as connection:
            connection.executescript(
                """
                CREATE TABLE deliveries(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_id TEXT NOT NULL UNIQUE,
                    note_id TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    trace_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(note_id, recipient, reason)
                );
                CREATE TABLE consumer_cursors(
                    principal TEXT NOT NULL,
                    consumer_id TEXT NOT NULL,
                    acked_sequence INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(principal, consumer_id)
                );
                CREATE TABLE issued_cursors(
                    cursor_hash TEXT PRIMARY KEY,
                    principal TEXT NOT NULL,
                    consumer_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    issued_at TEXT NOT NULL
                );
                PRAGMA user_version=3;
                """
            )
            for sequence, reason in enumerate(
                ("about", "direct", "handoff"), start=1
            ):
                connection.execute(
                    """
                    INSERT INTO deliveries(
                        sequence, delivery_id, note_id, recipient, reason,
                        trace_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, '', ?)
                    """,
                    (
                        sequence,
                        MemoryService._delivery_id(
                            note_id, RECEIVER.subject, reason
                        ),
                        note_id,
                        RECEIVER.subject,
                        reason,
                        "2026-07-25T00:00:00Z",
                    ),
                )
            if acknowledged:
                connection.execute(
                    """
                    INSERT INTO consumer_cursors VALUES (?, ?, ?, ?)
                    """,
                    (
                        RECEIVER.subject,
                        "desktop",
                        acknowledged,
                        "2026-07-25T00:00:00Z",
                    ),
                )
            connection.commit()
        return note_id

    def test_one_note_recipient_is_one_delivery_with_complete_reason_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = MemoryService(Path(temporary), enable_delivery=True)
            created = service.create_note(
                {
                    "type": "handoff",
                    "title": "One logical handoff",
                    "visibility": "direct:agent.beta",
                    "about": ["agent.beta"],
                    "body": "One content item with overlapping routing reasons.",
                },
                OWNER,
                idempotency_key="logical-handoff",
            )

            page = service.fetch_inbox(RECEIVER, "desktop.a")

            self.assertEqual(1, len(page["items"]))
            self.assertEqual(created.note["id"], page["items"][0]["note"]["id"])
            self.assertEqual(
                ["about", "direct", "handoff"],
                page["items"][0]["reasons"],
            )
            self.assertNotIn("reason", page["items"][0])

    def test_v1_partial_reason_page_does_not_create_logical_ack(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = MemoryService(Path(temporary), enable_delivery=True)
            created = service.create_note(
                {
                    "type": "handoff",
                    "title": "Compatibility page",
                    "visibility": "direct:agent.beta",
                    "about": ["agent.beta"],
                    "body": "three legacy rows",
                },
                OWNER,
                idempotency_key="v1-partial-page",
            )
            first = service.fetch_inbox(
                RECEIVER,
                "legacy.desktop",
                limit=1,
                delivery_model=DELIVERY_MODEL_V1,
            )
            service.acknowledge_inbox(
                RECEIVER, "legacy.desktop", first["cursor"]
            )

            lifecycle = service.note_lifecycle(created.note["id"], OWNER)
            self.assertFalse(
                lifecycle["facts"]["deliveries"][0]["acknowledged"]
            )
            self.assertEqual(
                1,
                len(
                    service.fetch_inbox(
                        RECEIVER, "legacy.desktop"
                    )["items"]
                ),
            )

            remainder = service.fetch_inbox(
                RECEIVER,
                "legacy.desktop",
                delivery_model=DELIVERY_MODEL_V1,
            )
            service.acknowledge_inbox(
                RECEIVER, "legacy.desktop", remainder["cursor"]
            )
            lifecycle = service.note_lifecycle(created.note["id"], OWNER)
            self.assertTrue(
                lifecycle["facts"]["deliveries"][0]["acknowledged"]
            )

    def test_future_ops_schema_fails_closed_without_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            ops = state / "memory" / "ops.sqlite"
            ops.parent.mkdir(parents=True)
            with closing(sqlite3.connect(ops)) as connection:
                connection.execute("CREATE TABLE future_marker(value TEXT NOT NULL)")
                connection.execute("INSERT INTO future_marker VALUES ('preserve')")
                connection.execute("PRAGMA user_version=99")
                connection.commit()

            with self.assertRaisesRegex(MemoryError, "newer.*schema|schema.*newer"):
                MemoryService(state, enable_delivery=True).init()

            with closing(sqlite3.connect(ops)) as connection:
                self.assertEqual(
                    99,
                    int(connection.execute("PRAGMA user_version").fetchone()[0]),
                )
                self.assertEqual(
                    "preserve",
                    connection.execute(
                        "SELECT value FROM future_marker"
                    ).fetchone()[0],
                )

    def test_v3_none_partial_and_full_ack_migrate_conservatively(self) -> None:
        for acknowledged, expected_count in ((0, 1), (1, 1), (3, 0)):
            with self.subTest(acknowledged=acknowledged):
                with tempfile.TemporaryDirectory() as temporary:
                    state = Path(temporary)
                    note_id = self._write_v3_fixture(state, acknowledged)
                    service = MemoryService(state, enable_delivery=True)

                    service.init()
                    page = service.fetch_inbox(RECEIVER, "desktop")

                    self.assertEqual(expected_count, len(page["items"]))
                    with closing(sqlite3.connect(service.ops_path)) as connection:
                        version = int(
                            connection.execute(
                                "PRAGMA user_version"
                            ).fetchone()[0]
                        )
                        logical = connection.execute(
                            """
                            SELECT sequence FROM logical_deliveries
                            WHERE note_id=? AND recipient=?
                            """,
                            (note_id, RECEIVER.subject),
                        ).fetchone()
                        aliases = connection.execute(
                            "SELECT COUNT(*) FROM delivery_aliases"
                        ).fetchone()[0]
                        reasons = [
                            row[0]
                            for row in connection.execute(
                                """
                                SELECT reason FROM logical_delivery_reasons
                                ORDER BY reason
                                """
                            )
                        ]
                    self.assertEqual(4, version)
                    self.assertEqual(3, logical[0])
                    self.assertEqual(3, aliases)
                    self.assertEqual(["about", "direct", "handoff"], reasons)
                    if acknowledged == 3:
                        lifecycle = service.note_lifecycle(note_id, OWNER)
                        delivery = lifecycle["facts"]["deliveries"][0]
                        self.assertTrue(delivery["acknowledged"])
                        self.assertIsNone(delivery["acknowledgedAt"])


if __name__ == "__main__":
    unittest.main()
