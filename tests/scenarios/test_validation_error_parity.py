from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:
    ClientSession = None

from a2a_superhub.client import HubClient, HubClientError
from a2a_superhub.server import make_server


ROOT = Path(__file__).resolve().parents[2]
PRINCIPALS = {
    "alpha-token": {
        "subject": "agent.alpha",
        "kind": "agent",
        "tokenId": "tok_alpha",
        "scopes": ["memory.read", "memory.write", "memory.share"],
    }
}
ALLOWED_NOTE_TYPES = [
    "note",
    "decision",
    "handoff",
    "observation",
    "task-log",
    "profile",
]


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


def raw_request(
    base_url: str,
    path: str,
    payload: dict,
    *,
    idempotency_key: str,
) -> tuple[int, dict]:
    request = urllib.request.Request(
        base_url + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer alpha-token",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


@asynccontextmanager
async def open_mcp(base_url: str):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "a2a_superhub.mcp_server"],
        cwd=str(ROOT),
        env={
            "PYTHONPATH": str(ROOT / "src"),
            "A2A_SUPERHUB_URL": base_url,
            "A2A_SUPERHUB_TOKEN": "alpha-token",
        },
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


class ValidationErrorParityScenarios(unittest.TestCase):
    def test_http_client_preserves_typed_safe_validation_details_without_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            server, thread = start_hub(temporary)
            base = f"http://127.0.0.1:{server.server_port}"
            client = HubClient(base, token="alpha-token")
            invalid = {
                "type": "issue",
                "title": "invalid type",
                "visibility": "private",
                "body": "credential-marker-must-not-return",
            }
            try:
                with self.assertRaises(HubClientError) as raised:
                    client.create_note(invalid, "same-key-after-rejection")
                error = raised.exception
                self.assertEqual(400, error.status)
                self.assertEqual("INVALID_REQUEST", error.code)
                self.assertIs(False, getattr(error, "retryable", None))
                self.assertRegex(
                    getattr(error, "trace_id", None) or "",
                    r"^trace_[0-9a-f]{32}$",
                )
                self.assertEqual(
                    {
                        "fieldPath": "type",
                        "rule": "enum",
                        "allowedValues": ALLOWED_NOTE_TYPES,
                    },
                    getattr(error, "details", None),
                )
                self.assertNotIn("issue", str(error))
                self.assertNotIn(
                    "credential-marker-must-not-return",
                    json.dumps(
                        {
                            "message": str(error),
                            "details": getattr(error, "details", None),
                            "traceId": getattr(error, "trace_id", None),
                        }
                    ),
                )

                created = client.create_note(
                    {
                        "type": "observation",
                        "title": "valid retry",
                        "visibility": "private",
                        "body": "valid",
                    },
                    "same-key-after-rejection",
                )
                self.assertTrue(created["id"].startswith("mem_"))
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_unexpected_exception_is_fixed_internal_error_without_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            server, thread = start_hub(temporary)
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with patch.object(
                    server.memory_service,
                    "create_note",
                    side_effect=RuntimeError(
                        "private-path-and-token-marker-must-not-return"
                    ),
                ):
                    status, response = raw_request(
                        base,
                        "/v2/memory/notes",
                        {
                            "type": "observation",
                            "title": "unexpected",
                            "visibility": "private",
                            "body": "request-body-marker-must-not-return",
                        },
                        idempotency_key="unexpected-error",
                    )
                self.assertEqual(500, status)
                self.assertEqual("INTERNAL_ERROR", response["error"]["code"])
                self.assertFalse(response["error"]["retryable"])
                serialized = json.dumps(response)
                self.assertNotIn("private-path", serialized)
                self.assertNotIn("request-body", serialized)
                self.assertRegex(response["traceId"], r"^trace_[0-9a-f]{32}$")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_v2_relation_target_requires_one_of_six_typed_namespaces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            server, thread = start_hub(temporary)
            client = HubClient(
                f"http://127.0.0.1:{server.server_port}", token="alpha-token"
            )
            try:
                with self.assertRaises(HubClientError) as raised:
                    client.create_note(
                        {
                            "type": "observation",
                            "title": "ambiguous relation",
                            "visibility": "private",
                            "relations": [
                                {
                                    "type": "references",
                                    "target": "ambiguous-secret-marker",
                                }
                            ],
                            "body": "relation-body-secret-marker",
                        },
                        "typed-target-retry",
                    )
                self.assertEqual(
                    "relations[0].target",
                    raised.exception.details["fieldPath"],
                )
                self.assertEqual(
                    "typed-namespace", raised.exception.details["rule"]
                )
                self.assertEqual(
                    [
                        "agent:<id>",
                        "artifact:<id>",
                        "event:<id>",
                        "note:<id>",
                        "project:<id>",
                        "task:<id>",
                    ],
                    raised.exception.details["allowedValues"],
                )
                self.assertNotIn(
                    "secret-marker",
                    json.dumps(
                        {
                            "message": str(raised.exception),
                            "details": raised.exception.details,
                        }
                    ),
                )
                created = client.create_note(
                    {
                        "type": "observation",
                        "title": "canonical relation",
                        "visibility": "private",
                        "relations": [
                            {
                                "type": "references",
                                "target": "project:memory-contract",
                            }
                        ],
                        "body": "valid",
                    },
                    "typed-target-retry",
                )
                self.assertTrue(created["id"].startswith("mem_"))
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()


@unittest.skipIf(ClientSession is None, "install the MCP extra for stdio parity")
class McpValidationErrorParityScenarios(unittest.IsolatedAsyncioTestCase):
    async def test_actual_mcp_preserves_http_validation_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            server, thread = start_hub(temporary)
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                async with open_mcp(base) as session:
                    result = await session.call_tool(
                        "memory_write",
                        {
                            "type": "issue",
                            "title": "invalid MCP type",
                            "visibility": "private",
                            "body": "mcp-secret-marker",
                            "idempotencyKey": "mcp-invalid-type",
                        },
                    )
                    self.assertTrue(result.isError)
                    error = result.structuredContent["error"]
                    self.assertEqual("INVALID_REQUEST", error["code"])
                    self.assertEqual(400, error["status"])
                    self.assertIs(False, error.get("retryable"))
                    self.assertEqual("type", (error.get("details") or {}).get("fieldPath"))
                    self.assertEqual("enum", (error.get("details") or {}).get("rule"))
                    self.assertEqual(
                        ALLOWED_NOTE_TYPES,
                        (error.get("details") or {}).get("allowedValues"),
                    )
                    self.assertRegex(
                        error.get("traceId") or "",
                        r"^trace_[0-9a-f]{32}$",
                    )
                    self.assertNotIn("mcp-secret-marker", json.dumps(error))
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    async def test_memory_read_can_include_authorized_lifecycle_without_new_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            server, thread = start_hub(temporary)
            base = f"http://127.0.0.1:{server.server_port}"
            client = HubClient(base, token="alpha-token")
            try:
                created = client.create_note(
                    {
                        "type": "observation",
                        "title": "Lifecycle through MCP",
                        "visibility": "private",
                        "body": "facts",
                    },
                    "mcp-lifecycle",
                )
                async with open_mcp(base) as session:
                    result = await session.call_tool(
                        "memory_read",
                        {
                            "id": created["id"],
                            "includeLifecycle": True,
                        },
                    )
                self.assertFalse(result.isError)
                self.assertEqual(
                    created["id"],
                    result.structuredContent["note"]["id"],
                )
                lifecycle = result.structuredContent["lifecycle"]
                self.assertEqual(
                    "a2a-superhub.memory.lifecycle.v1",
                    lifecycle["schema"],
                )
                self.assertNotIn("state", lifecycle)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
