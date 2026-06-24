from __future__ import annotations

import tempfile
import unittest

from a2a_superhub.artifacts import ArtifactStore
from a2a_superhub.store import HubStore


class StoreAndArtifactTests(unittest.TestCase):
    def test_task_idempotency_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = HubStore(tmp)
            task, inserted = store.create_task(
                {
                    "fromAgent": "agent.alpha",
                    "toAgent": "agent.beta",
                    "intent": "agent.query",
                    "idempotencyKey": "same-request",
                    "payload": {"summary": "hello"},
                }
            )
            self.assertTrue(inserted)
            duplicate, inserted_again = store.create_task(
                {
                    "fromAgent": "agent.alpha",
                    "toAgent": "agent.beta",
                    "intent": "agent.query",
                    "idempotencyKey": "same-request",
                    "payload": {"summary": "different"},
                }
            )
            self.assertFalse(inserted_again)
            self.assertEqual(task["taskId"], duplicate["taskId"])

            event = store.append_event(task["taskId"], "task.progress", {"message": "working"}, state="working")
            self.assertEqual(event["state"], "working")
            self.assertEqual(store.get_task(task["taskId"])["state"], "working")
            self.assertEqual(len(store.list_events(task["taskId"])), 2)

    def test_agent_registration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = HubStore(tmp)
            registered = store.register_agent({"id": "agent.alpha", "name": "Alpha"})
            self.assertEqual(registered["agentId"], "agent.alpha")
            self.assertEqual(store.list_agents()[0]["agentId"], "agent.alpha")

    def test_artifact_roundtrip_and_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts = ArtifactStore(tmp)
            manifest = artifacts.put_bytes(b"hello artifact", filename="hello.txt", media_type="text/plain", created_by="agent.alpha")
            self.assertEqual(manifest["sizeBytes"], len(b"hello artifact"))
            self.assertEqual(artifacts.get_manifest(manifest["artifactId"])["sha256"], manifest["sha256"])
            self.assertEqual(artifacts.get_bytes(manifest["artifactId"]), b"hello artifact")


if __name__ == "__main__":
    unittest.main()
