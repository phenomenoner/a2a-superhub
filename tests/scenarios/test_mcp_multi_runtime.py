from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

try:
    import anyio
    from mcp import ClientSession, StdioServerParameters, types
    from mcp.client.stdio import stdio_client
    from pydantic import AnyUrl
except ImportError:
    anyio = None
    ClientSession = None

from a2a_superhub.client import HubClient
from a2a_superhub.server import make_server


ROOT = Path(__file__).resolve().parents[2]
PRINCIPALS = {
    "alpha-token": {
        "subject": "agent.alpha",
        "kind": "agent",
        "tokenId": "tok_alpha",
        "scopes": ["memory.read", "memory.write", "memory.share", "task.read", "task.write"],
    },
    "beta-token": {
        "subject": "agent.beta",
        "kind": "agent",
        "tokenId": "tok_beta",
        "scopes": ["memory.read", "task.read"],
    },
}


def start_hub(state: str):
    server = make_server(
        state,
        port=0,
        enable_memory=True,
        enable_delivery=True,
        principals=PRINCIPALS,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@asynccontextmanager
async def open_mcp(base_url: str, token: str, message_handler=None):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "a2a_superhub.mcp_server"],
        cwd=str(ROOT),
        env={
            "PYTHONPATH": str(ROOT / "src"),
            "A2A_SUPERHUB_URL": base_url,
            "A2A_SUPERHUB_TOKEN": token,
        },
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(
            read_stream,
            write_stream,
            message_handler=message_handler,
        ) as session:
            initialized = await session.initialize()
            yield session, initialized


@unittest.skipIf(ClientSession is None, "install the mcp extra for stdio scenarios")
class McpMultiRuntimeScenarios(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_lifecycle_tools_resources_and_hub_auth_denial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server, thread = start_hub(tmp)
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                async with open_mcp(base, "alpha-token") as (session, initialized):
                    self.assertEqual("2025-11-25", initialized.protocolVersion)
                    self.assertTrue(initialized.capabilities.resources.subscribe)
                    listed = await session.list_tools()
                    self.assertEqual(10, len(listed.tools))
                    created = await session.call_tool(
                        "memory_write",
                        {
                            "type": "observation",
                            "title": "MCP lifecycle",
                            "visibility": "private",
                            "body": "stdio marker",
                            "idempotencyKey": "mcp-stdio-note",
                        },
                    )
                    self.assertFalse(created.isError)
                    note_id = created.structuredContent["id"]
                    self.assertEqual("agent.alpha", created.structuredContent["author"])
                    resource = await session.read_resource(AnyUrl(f"memory://note/{note_id}"))
                    note = json.loads(resource.contents[0].text)
                    self.assertEqual(note_id, note["id"])
                    self.assertEqual("agent.alpha", note["author"])

                    task = await session.call_tool(
                        "task_create",
                        {
                            "fromAgent": "agent.alpha",
                            "toAgent": "agent.beta",
                            "intent": "observe.gateway",
                            "idempotencyKey": "mcp-stdio-task",
                            "payload": {"summary": "observe"},
                            "permissions": {"sideEffects": "default-deny", "scopes": []},
                        },
                    )
                    task_id = task.structuredContent["task"]["taskId"]
                    status = await session.call_tool("task_status", {"taskId": task_id})
                    self.assertEqual(task_id, status.structuredContent["taskId"])

                async with open_mcp(base, "beta-token") as (session, _initialized):
                    denied = await session.call_tool(
                        "memory_write",
                        {
                            "type": "observation",
                            "title": "must fail",
                            "visibility": "private",
                            "body": "denied marker",
                            "idempotencyKey": "mcp-denied-write",
                        },
                    )
                    self.assertTrue(denied.isError)
                    self.assertEqual("auth", denied.structuredContent["error"]["kind"])
                    self.assertEqual(403, denied.structuredContent["error"]["status"])
                search = HubClient(base, token="alpha-token").search("denied marker")
                self.assertEqual([], search["items"])
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    async def test_http_to_mcp_offline_handoff_subscription_provenance_and_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first, first_thread = start_hub(tmp)
            first_base = f"http://127.0.0.1:{first.server_port}"
            alpha = HubClient(first_base, token="alpha-token")
            try:
                persisted = alpha.create_note(
                    {
                        "type": "handoff",
                        "title": "Offline handoff",
                        "visibility": "direct:agent.beta",
                        "participants": ["agent.alpha", "agent.beta"],
                        "about": ["agent.beta"],
                        "relations": [{"type": "x-source-task", "target": "task:task_offline"}],
                        "body": "restart-safe observation",
                    },
                    "http-before-restart",
                )
            finally:
                first.shutdown()
                first_thread.join(timeout=5)
                first.server_close()

            second, second_thread = start_hub(tmp)
            second_base = f"http://127.0.0.1:{second.server_port}"
            notifications: list[object] = []
            updated = anyio.Event()

            async def handle_message(message):
                notifications.append(message)
                if isinstance(message, types.ServerNotification) and isinstance(
                    message.root, types.ResourceUpdatedNotification
                ):
                    updated.set()

            try:
                async with open_mcp(second_base, "beta-token", handle_message) as (session, _initialized):
                    inbox = await session.call_tool("memory_inbox", {"consumerId": "agent.beta"})
                    self.assertIn(persisted["id"], json.dumps(inbox.structuredContent, sort_keys=True))
                    wakeup = await session.read_resource(AnyUrl("memory://wakeup/agent.beta"))
                    envelope = json.loads(wakeup.contents[0].text)
                    self.assertEqual("data", envelope["role"])
                    self.assertEqual("untrusted-memory", envelope["trust"])
                    serialized = json.dumps(envelope, sort_keys=True)
                    self.assertIn(persisted["id"], serialized)
                    self.assertIn("task:task_offline", serialized)

                    await session.subscribe_resource(AnyUrl("memory://wakeup/agent.beta"))
                    alpha_after_restart = HubClient(second_base, token="alpha-token")
                    changed = await anyio.to_thread.run_sync(
                        lambda: alpha_after_restart.create_note(
                            {
                                "type": "observation",
                                "title": "Subscription update",
                                "visibility": "direct:agent.beta",
                                "participants": ["agent.alpha", "agent.beta"],
                                "about": ["agent.beta"],
                                "body": "resource changed",
                            },
                            "http-subscription-update",
                        )
                    )
                    with anyio.fail_after(8):
                        await updated.wait()
                    resource_updates = [
                        item
                        for item in notifications
                        if isinstance(item, types.ServerNotification)
                        and isinstance(item.root, types.ResourceUpdatedNotification)
                    ]
                    self.assertTrue(resource_updates)
                    refreshed = await session.read_resource(AnyUrl("memory://wakeup/agent.beta"))
                    refreshed_envelope = json.loads(refreshed.contents[0].text)
                    self.assertIn(changed["id"], json.dumps(refreshed_envelope, sort_keys=True))

                    fetched = await session.call_tool("memory_inbox", {"consumerId": "agent.beta"})
                    acked = await session.call_tool(
                        "memory_inbox_ack",
                        {"consumerId": "agent.beta", "cursor": fetched.structuredContent["cursor"]},
                    )
                    self.assertTrue(acked.structuredContent["acked"])
                    after = await session.call_tool("memory_inbox", {"consumerId": "agent.beta"})
                    self.assertEqual([], after.structuredContent["items"])
            finally:
                second.shutdown()
                second_thread.join(timeout=5)
                second.server_close()


if __name__ == "__main__":
    unittest.main()
