from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from a2a_superhub.auth import Principal
from a2a_superhub.memory import AuthorizationError, MemoryService


OWNER = Principal("agent.alpha", "agent", "tok_owner", frozenset({"memory.read", "memory.write", "memory.share"}))
BETA = Principal("agent.beta", "agent", "tok_beta", frozenset({"memory.read", "memory.write", "memory.share"}))
OTHER = Principal("agent.gamma", "agent", "tok_other", frozenset({"memory.read"}))
ADMIN = Principal("local.operator", "operator", "tok_admin", frozenset({"memory.read", "memory.write", "memory.share", "memory.admin"}))


class MemoryViewsTests(unittest.TestCase):
    def _service(self, root: Path) -> MemoryService:
        values = iter(f"mem_{index:032x}" for index in range(100, 500))
        return MemoryService(root, new_note_id=lambda: next(values), enable_delivery=True)

    def test_timeline_project_pair_about_and_graph_final_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            shared = service.create_note(
                {
                    "type": "observation", "title": "shared graph", "visibility": "shared", "project": "gateway",
                    "participants": ["agent.beta"], "about": ["agent.beta"],
                    "relations": [{"type": "depends_on", "target": "project:runtime"}], "body": "shared snippet",
                },
                OWNER,
                idempotency_key="shared-graph",
            )
            private = service.create_note(
                {
                    "type": "observation", "title": "private graph", "visibility": "private", "project": "gateway",
                    "about": ["agent.beta"], "relations": [{"type": "blocks", "target": "project:secret"}], "body": "private snippet",
                },
                OWNER,
                idempotency_key="private-graph",
            )

            timeline = service.timeline(OTHER, project="gateway")
            self.assertEqual([shared.note["id"]], [item["id"] for item in timeline])
            self.assertEqual([shared.note["id"]], [item["id"] for item in service.timeline(OTHER, pair=("agent.alpha", "agent.beta"))])
            self.assertEqual([shared.note["id"]], [item["id"] for item in service.timeline(OTHER, about="agent.beta")])
            graph = service.graph(OTHER, "agent:agent.beta", hops=2)
            serialized = json.dumps(graph)
            self.assertIn(shared.note["id"], serialized)
            self.assertNotIn(private.note["id"], serialized)
            self.assertNotIn("private snippet", serialized)
            self.assertNotIn("project:secret", serialized)
            admin_graph = service.graph(ADMIN, "agent:agent.beta", hops=2)
            self.assertIn(private.note["id"], json.dumps(admin_graph))

    def test_supersedes_same_author_or_admin_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            original = service.create_note(
                {"type": "decision", "title": "old", "visibility": "shared", "body": "old decision"},
                OWNER,
                idempotency_key="old",
            )
            with self.assertRaises(AuthorizationError):
                service.create_note(
                    {"type": "decision", "title": "unauthorized", "visibility": "shared", "supersedes": original.note["id"], "body": "no"},
                    BETA,
                    idempotency_key="bad-supersede",
                )
            replacement = service.create_note(
                {
                    "type": "decision", "title": "new", "visibility": "shared", "supersedes": original.note["id"],
                    "relations": [{"type": "disputes", "target": f"note:{original.note['id']}"}], "body": "new decision",
                },
                OWNER,
                idempotency_key="new",
            )
            current = service.timeline(OTHER)
            self.assertEqual([replacement.note["id"]], [item["id"] for item in current])
            history = service.timeline(OTHER, include_superseded=True)
            status = {item["id"]: item["temporalStatus"] for item in history}
            self.assertEqual("superseded", status[original.note["id"]])
            self.assertEqual("current", status[replacement.note["id"]])

    def test_private_successor_cannot_hide_shared_predecessor_from_other(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            original = service.create_note(
                {"type": "decision", "title": "public old", "visibility": "shared", "body": "visible"},
                OWNER,
                idempotency_key="public-old",
            )
            replacement = service.create_note(
                {"type": "decision", "title": "private new", "visibility": "private", "supersedes": original.note["id"], "body": "hidden"},
                OWNER,
                idempotency_key="private-new",
            )

            other = service.timeline(OTHER, include_superseded=True)
            self.assertEqual([(original.note["id"], "current")], [(item["id"], item["temporalStatus"]) for item in other])
            self.assertNotIn(replacement.note["id"], json.dumps(service.graph(OTHER, f"note:{original.note['id']}", hops=2)))
            owner_status = {item["id"]: item["temporalStatus"] for item in service.timeline(OWNER, include_superseded=True)}
            self.assertEqual("superseded", owner_status[original.note["id"]])
            self.assertEqual("current", owner_status[replacement.note["id"]])

    def test_shared_relation_to_private_note_has_zero_node_and_edge_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            private = service.create_note(
                {"type": "observation", "title": "private target", "visibility": "private", "body": "hidden"},
                OWNER,
                idempotency_key="private-target",
            )
            shared = service.create_note(
                {
                    "type": "observation", "title": "shared ref", "visibility": "shared", "body": "visible",
                    "relations": [{"type": "references", "target": f"note:{private.note['id']}"}],
                },
                OWNER,
                idempotency_key="shared-ref",
            )
            hidden_query = service.graph(OTHER, f"note:{private.note['id']}", hops=2)
            self.assertEqual({"nodes": [], "edges": []}, hidden_query)
            self.assertNotIn(private.note["id"], json.dumps(hidden_query))
            shared_query = json.dumps(service.graph(OTHER, f"note:{shared.note['id']}", hops=1))
            self.assertNotIn(private.note["id"], shared_query)

    def test_fts_invalid_query_private_filter_and_rebuild_parity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            shared = service.create_note(
                {"type": "observation", "title": "search", "visibility": "shared", "body": "quartzneedle exact"},
                OWNER,
                idempotency_key="fts-shared",
            )
            service.create_note(
                {"type": "observation", "title": "private", "visibility": "private", "body": "quartzneedle secret"},
                OWNER,
                idempotency_key="fts-private",
            )
            self.assertEqual([shared.note["id"]], [item["id"] for item in service.search("quartzneedle", OTHER)])
            self.assertIsInstance(service.search('" OR ( ***', OTHER), list)
            service.rebuild_index()
            self.assertEqual([shared.note["id"]], [item["id"] for item in service.search("quartzneedle", OTHER)])

    def test_fts_candidate_visibility_prevents_private_crowd_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            for index in range(12):
                service.create_note(
                    {"type": "observation", "title": f"private {index}", "visibility": "private", "body": "crowdterm"},
                    OWNER, idempotency_key=f"crowd-private-{index}",
                )
            shared = service.create_note(
                {"type": "observation", "title": "shared", "visibility": "shared", "body": "crowdterm"},
                OWNER, idempotency_key="crowd-shared",
            )
            result = service.search("crowdterm", OTHER, limit=1)
            self.assertEqual([shared.note["id"]], [item["id"] for item in result])
            self.assertNotIn("private", json.dumps(result))

    def test_stats_and_receipts_are_admin_only_and_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            created = service.create_note(
                {"type": "observation", "title": "receipt", "visibility": "shared", "about": ["agent.beta"], "body": "TOP SECRET TOKEN"},
                OWNER,
                idempotency_key="receipt",
                trace_id="trace_sanitized001",
            )
            fetched = service.fetch_inbox(BETA, "desktop.a")
            service.acknowledge_inbox(BETA, "desktop.a", fetched["cursor"])
            with self.assertRaises(AuthorizationError):
                service.stats(OTHER)
            stats = service.stats(ADMIN)
            receipts = service.list_receipts(trace_id="trace_sanitized001")
            serialized = json.dumps({"stats": stats, "receipts": receipts})
            self.assertIn(created.note["id"], serialized)
            self.assertNotIn("TOP SECRET TOKEN", serialized)
            self.assertNotIn("tok_owner", serialized)
            self.assertNotIn(str(tmp), serialized)
            self.assertEqual({"write", "index", "delivery", "ack"}, {item["phase"] for item in receipts})


if __name__ == "__main__":
    unittest.main()
