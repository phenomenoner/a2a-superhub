from __future__ import annotations

import json
import sys
import threading
import unittest
from contextlib import asynccontextmanager, contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:
    ClientSession = None

from a2a_superhub.client import HubClient, HubClientError


ROOT = Path(__file__).resolve().parents[1]
SAFE_DETAILS = {
    "fieldPath": "type",
    "rule": "enum",
    "allowedValues": ["note", "decision", "handoff", "observation", "task-log", "profile"],
}
TYPED_ERROR = {
    "error": {
        "code": "INVALID_REQUEST",
        "message": "note type is not supported",
        "retryable": False,
        "details": SAFE_DETAILS,
    },
    "traceId": "trace_0123456789abcdef0123456789abcdef",
}


@contextmanager
def error_endpoint(status: HTTPStatus, response: dict[str, Any]) -> Iterator[str]:
    class ErrorHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                self.rfile.read(length)
            body = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), ErrorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@asynccontextmanager
async def open_mcp(base_url: str):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "a2a_superhub.mcp_server"],
        cwd=str(ROOT),
        env={
            "PYTHONPATH": str(ROOT / "src"),
            "A2A_SUPERHUB_URL": base_url,
        },
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


class HubClientErrorFidelityTests(unittest.TestCase):
    def test_typed_http_error_preserves_safe_server_contract(self) -> None:
        with error_endpoint(HTTPStatus.BAD_REQUEST, TYPED_ERROR) as base_url:
            client = HubClient(base_url)
            with self.assertRaises(HubClientError) as raised:
                client.create_note(
                    {
                        "type": "issue",
                        "title": "invalid",
                        "visibility": "private",
                        "body": "not returned",
                    },
                    "typed-error",
                )

        error = raised.exception
        self.assertEqual("note type is not supported", str(error))
        self.assertEqual("http", error.kind)
        self.assertEqual(400, error.status)
        self.assertEqual("INVALID_REQUEST", error.code)
        self.assertIs(False, error.retryable)
        self.assertEqual(SAFE_DETAILS, error.details)
        self.assertEqual(TYPED_ERROR["traceId"], error.trace_id)

    def test_legacy_string_error_keeps_existing_classification_and_message(self) -> None:
        with error_endpoint(HTTPStatus.FORBIDDEN, {"error": "legacy denial"}) as base_url:
            with self.assertRaises(HubClientError) as raised:
                HubClient(base_url).create_note({}, "legacy-error")

        error = raised.exception
        self.assertEqual("hub request failed (403 legacy denial)", str(error))
        self.assertEqual("auth", error.kind)
        self.assertEqual(403, error.status)
        self.assertEqual("legacy denial", error.code)
        self.assertIsNone(error.retryable)
        self.assertIsNone(error.details)
        self.assertIsNone(error.trace_id)

    def test_oversized_details_are_not_forwarded(self) -> None:
        response = {
            **TYPED_ERROR,
            "error": {
                **TYPED_ERROR["error"],
                "details": {"allowedValues": ["x" * 10_000]},
            },
        }
        with error_endpoint(HTTPStatus.BAD_REQUEST, response) as base_url:
            with self.assertRaises(HubClientError) as raised:
                HubClient(base_url).create_note({}, "oversized-details")

        error = raised.exception
        self.assertEqual("INVALID_REQUEST", error.code)
        self.assertEqual("note type is not supported", str(error))
        self.assertIsNone(error.details)
        self.assertEqual(TYPED_ERROR["traceId"], error.trace_id)


@unittest.skipIf(ClientSession is None, "install the MCP extra for stdio error fidelity")
class McpErrorFidelityTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_mcp_preserves_typed_http_error_data(self) -> None:
        with error_endpoint(HTTPStatus.BAD_REQUEST, TYPED_ERROR) as base_url:
            async with open_mcp(base_url) as session:
                result = await session.call_tool(
                    "memory_write",
                    {
                        "type": "issue",
                        "title": "invalid",
                        "visibility": "private",
                        "body": "not returned",
                        "idempotencyKey": "mcp-typed-error",
                    },
                )

        self.assertTrue(result.isError)
        expected = {
            "kind": "http",
            "status": 400,
            "code": "INVALID_REQUEST",
            "message": "note type is not supported",
            "retryable": False,
            "details": SAFE_DETAILS,
            "traceId": TYPED_ERROR["traceId"],
        }
        self.assertEqual(expected, result.structuredContent["error"])
        self.assertEqual({"error": expected}, json.loads(result.content[0].text))


if __name__ == "__main__":
    unittest.main()
