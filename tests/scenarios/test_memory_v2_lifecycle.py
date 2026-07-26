from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from a2a_superhub.auth import Principal
from a2a_superhub.memory import (
    CURSOR_PURPOSE_UNKNOWN,
    DELIVERY_MODEL_V1,
    AuthorizationError,
    CursorRefreshRequiredError,
    MemoryService,
    atomic_write,
    note_path,
    serialize_note,
)
from a2a_superhub.server import make_server


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
OWNER_WITHOUT_READ = Principal(
    "agent.alpha", "agent", "tok_owner_without_read", frozenset()
)
RECEIVER_WITHOUT_READ = Principal(
    "agent.beta", "agent", "tok_receiver_without_read", frozenset()
)
ADMIN_WITHOUT_READ = Principal(
    "local.operator", "operator", "tok_admin_without_read", frozenset({"memory.admin"})
)


class MemoryV2LifecycleScenarios(unittest.TestCase):
    def test_lifecycle_requires_read_scope_for_author_and_recipient_on_service_and_http(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = MemoryService(Path(temporary), enable_delivery=True)
            created = service.create_note(
                {
                    "type": "handoff",
                    "title": "Scope-protected lifecycle",
                    "visibility": "direct:agent.beta",
                    "body": "facts require authority",
                },
                OWNER,
                idempotency_key="lifecycle-scope",
            )

            for principal in (OWNER_WITHOUT_READ, RECEIVER_WITHOUT_READ):
                with self.subTest(surface="service", subject=principal.subject):
                    with self.assertRaises(AuthorizationError):
                        service.note_lifecycle(created.note["id"], principal)
            self.assertEqual(
                created.note["id"],
                service.note_lifecycle(created.note["id"], ADMIN_WITHOUT_READ)[
                    "noteId"
                ],
            )

            principals = {
                "owner-without-read": {
                    "subject": OWNER_WITHOUT_READ.subject,
                    "kind": "agent",
                    "tokenId": OWNER_WITHOUT_READ.token_id,
                    "scopes": ["task.read"],
                },
                "receiver-without-read": {
                    "subject": RECEIVER_WITHOUT_READ.subject,
                    "kind": "agent",
                    "tokenId": RECEIVER_WITHOUT_READ.token_id,
                    "scopes": ["task.read"],
                },
                "admin-without-read": {
                    "subject": ADMIN_WITHOUT_READ.subject,
                    "kind": "operator",
                    "tokenId": ADMIN_WITHOUT_READ.token_id,
                    "scopes": ["memory.admin"],
                },
            }
            server = make_server(
                temporary,
                port=0,
                enable_memory=True,
                enable_delivery=True,
                principals=principals,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = (
                f"http://127.0.0.1:{server.server_port}"
                f"/v2/memory/notes/{created.note['id']}/lifecycle"
            )
            try:
                for token in ("owner-without-read", "receiver-without-read"):
                    with self.subTest(surface="http", token=token):
                        request = urllib.request.Request(
                            url,
                            headers={"Authorization": f"Bearer {token}"},
                        )
                        with self.assertRaises(urllib.error.HTTPError) as raised:
                            urllib.request.urlopen(request, timeout=10)
                        self.assertEqual(403, raised.exception.code)
                        body = json.loads(
                            raised.exception.read().decode("utf-8")
                        )
                        self.assertEqual("SCOPE_DENIED", body["error"]["code"])

                admin_request = urllib.request.Request(
                    url,
                    headers={"Authorization": "Bearer admin-without-read"},
                )
                with urllib.request.urlopen(admin_request, timeout=10) as response:
                    admin_view = json.loads(response.read().decode("utf-8"))
                self.assertEqual(created.note["id"], admin_view["noteId"])
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

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
