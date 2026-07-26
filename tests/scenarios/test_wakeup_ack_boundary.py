from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from a2a_superhub.adapter import ReferenceAdapter
from a2a_superhub.auth import Principal
from a2a_superhub.memory import MemoryService


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


class ServiceBackedClient:
    def __init__(self, service: MemoryService):
        self.service = service
        self.ack_calls: list[tuple[str, str]] = []

    def negotiate(self) -> dict[str, Any]:
        return {
            "schema": "a2a-superhub.capabilities.v1",
            "memoryFoundation": True,
            "memorySharing": True,
            "safeWakeup": True,
            "adapter": True,
            "wakeupAckMode": "none",
            "ackCursorSource": "inbox-only",
            "principal": {
                "subject": RECEIVER.subject,
                "scopes": ["memory.read"],
            },
        }

    def wakeup(self, consumer_id: str, *, budget_bytes: int) -> dict[str, Any]:
        return self.service.wakeup(RECEIVER, consumer_id, budget_bytes=budget_bytes)

    def ack_inbox(self, consumer_id: str, cursor: str) -> dict[str, Any]:
        self.ack_calls.append((consumer_id, cursor))
        return self.service.acknowledge_inbox(RECEIVER, consumer_id, cursor)


class WakeupAcknowledgementBoundaryScenarios(unittest.TestCase):
    def _service_with_backlog(self, root: Path, count: int = 5) -> MemoryService:
        ids = iter(f"mem_{index:032x}" for index in range(1, count + 1))
        service = MemoryService(
            root,
            enable_delivery=True,
            new_note_id=lambda: next(ids),
        )
        for index in range(count):
            service.create_note(
                {
                    "type": "observation",
                    "title": f"pending {index}",
                    "visibility": "shared",
                    "about": [RECEIVER.subject],
                    "body": "bounded wakeup payload " * 40,
                },
                OWNER,
                idempotency_key=f"pending-{index}",
            )
        return service

    def _partial_budget(self, service: MemoryService, consumer_id: str) -> int:
        pending = len(service.fetch_inbox(RECEIVER, consumer_id)["items"])
        for budget in range(1024, 65_537, 128):
            envelope = service.wakeup(RECEIVER, consumer_id, budget_bytes=budget)
            shown = len(envelope.get("items", []))
            if envelope.get("truncated") and 0 < shown < pending:
                return budget
        self.fail("fixture did not produce a partial bounded wakeup envelope")

    def test_truncated_wakeup_delivery_never_acknowledges_omitted_inbox_items(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self._service_with_backlog(Path(temporary))
            consumer_id = "desktop.startup"
            before = service.fetch_inbox(RECEIVER, consumer_id)
            budget = self._partial_budget(service, consumer_id)
            client = ServiceBackedClient(service)
            adapter = ReferenceAdapter(
                client, principal=RECEIVER.subject, consumer_id=consumer_id
            )
            delivered: list[dict[str, Any]] = []

            result = adapter.start_session(delivered.append, budget_bytes=budget)

            self.assertEqual(1, len(delivered))
            self.assertEqual([], client.ack_calls)
            self.assertEqual(
                {"performed": False, "reason": "wakeup-preview"},
                result["ack"],
            )
            restarted = MemoryService(Path(temporary), enable_delivery=True)
            after = restarted.fetch_inbox(RECEIVER, consumer_id)
            self.assertEqual(
                [item["deliveryId"] for item in before["items"]],
                [item["deliveryId"] for item in after["items"]],
            )

    def test_wakeup_has_no_acknowledgeable_cursor_even_when_every_item_fits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self._service_with_backlog(Path(temporary), count=1)

            envelope = service.wakeup(RECEIVER, "desktop.startup")

            self.assertNotIn("cursor", envelope)
            self.assertEqual(
                "read-inbox" if envelope["truncated"] else None,
                envelope.get("nextAction"),
            )

    def test_wakeup_reports_authorized_backlog_beyond_page_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self._service_with_backlog(Path(temporary), count=101)

            envelope = service.wakeup(
                RECEIVER, "desktop.startup", budget_bytes=65_536
            )

            inbox = next(
                section
                for section in envelope["sections"]
                if section["kind"] == "inbox"
            )
            self.assertTrue(envelope["truncated"])
            self.assertTrue(inbox["hasMore"])
            self.assertIn("item-limit", envelope["truncation"]["reasons"])
            self.assertEqual(
                "read-inbox", envelope["truncation"]["nextAction"]
            )


if __name__ == "__main__":
    unittest.main()
