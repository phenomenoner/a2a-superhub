from __future__ import annotations

import base64
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from a2a_superhub.server import make_server


def request_json(url: str, payload: dict | None = None, token: str | None = None, headers: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", **(headers or {})}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


class ServerTests(unittest.TestCase):
    def test_non_loopback_requires_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "non-loopback"):
                make_server(tmp, host="0.0.0.0", port=0)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "invalid subject"):
                make_server(tmp, port=0, principals={"secret": {"subject": "INVALID", "scopes": ["memory.read"]}})
        with tempfile.TemporaryDirectory() as tmp:
            httpd = make_server(tmp, host="127.0.0.2", port=0)
            httpd.server_close()

    def test_json_rpc_task_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            httpd = make_server(tmp, port=0)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{httpd.server_port}"
            try:
                health = request_json(base + "/healthz")
                self.assertEqual(health["status"], "ready")
                capabilities = request_json(base + "/v1/capabilities")
                self.assertFalse(capabilities["memoryFoundation"])
                with self.assertRaises(urllib.error.HTTPError) as disabled_memory:
                    request_json(base + "/v1/memory/notes/mem_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
                self.assertEqual(404, disabled_memory.exception.code)

                created = request_json(
                    base + "/a2a",
                    {
                        "jsonrpc": "2.0",
                        "id": "req-1",
                        "method": "message/send",
                        "params": {
                            "fromAgent": "agent.alpha",
                            "toAgent": "agent.beta",
                            "idempotencyKey": "rpc-demo",
                            "payload": {"summary": "hello"},
                        },
                    },
                )
                task_id = created["result"]["task"]["taskId"]
                fetched = request_json(base + "/a2a", {"jsonrpc": "2.0", "id": "req-2", "method": "tasks/get", "params": {"id": task_id}})
                self.assertEqual(fetched["result"]["taskId"], task_id)
            finally:
                httpd.shutdown()
                thread.join(timeout=5)
                httpd.server_close()

    def test_bearer_auth_and_artifact_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            httpd = make_server(tmp, port=0, token="secret")
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{httpd.server_port}"
            try:
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    request_json(base + "/v1/tasks")
                self.assertEqual(raised.exception.code, 401)
                with self.assertRaises(urllib.error.HTTPError) as wrong:
                    request_json(base + "/v1/tasks", token="do-not-echo")
                self.assertNotIn("do-not-echo", wrong.exception.read().decode("utf-8"))

                manifest = request_json(
                    base + "/v1/artifacts",
                    {
                        "filename": "hello.txt",
                        "mediaType": "text/plain",
                        "createdBy": "agent.alpha",
                        "contentBase64": base64.b64encode(b"hello").decode("ascii"),
                    },
                    token="secret",
                )
                self.assertEqual(manifest["sizeBytes"], 5)
            finally:
                httpd.shutdown()
                thread.join(timeout=5)
                httpd.server_close()

    def test_memory_http_create_read_and_final_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            principals = {
                "owner-token": {"subject": "agent.alpha", "kind": "agent", "tokenId": "tok_owner", "scopes": ["memory.read", "memory.write", "memory.share"]},
                "other-token": {"subject": "agent.gamma", "kind": "agent", "tokenId": "tok_other", "scopes": ["memory.read"]},
                "writer-token": {"subject": "agent.writer", "kind": "agent", "tokenId": "tok_writer", "scopes": ["memory.write"]},
            }
            httpd = make_server(tmp, port=0, enable_memory=True, principals=principals)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{httpd.server_port}"
            try:
                capabilities = request_json(base + "/v1/capabilities", token="owner-token")
                self.assertFalse(capabilities["memorySharing"])
                self.assertFalse(capabilities["taskLog"])
                self.assertFalse(capabilities["watcherSideEffects"])
                self.assertFalse(capabilities["memoryFull"])
                created = request_json(
                    base + "/v1/memory/notes",
                    {"type": "note", "title": "HTTP note", "visibility": "direct:agent.gamma", "body": "hello"},
                    token="owner-token",
                    headers={"Idempotency-Key": "http-idem"},
                )
                note_id = created["id"]
                self.assertEqual("agent.alpha", created["author"])
                with self.assertRaises(urllib.error.HTTPError) as spoof:
                    request_json(
                        base + "/v1/memory/notes",
                        {"type": "note", "title": "spoof", "visibility": "private", "author": "agent.gamma", "body": "no"},
                        token="owner-token",
                    )
                self.assertEqual(400, spoof.exception.code)
                fetched = request_json(base + f"/v1/memory/notes/{note_id}", token="other-token")
                self.assertEqual(note_id, fetched["id"])
                self.assertEqual([], request_json(base + "/v1/memory/inbox?consumerId=agent.gamma", token="other-token")["items"])
                replay = request_json(
                    base + "/v1/memory/notes",
                    {"type": "note", "title": "HTTP note", "visibility": "direct:agent.gamma", "body": "hello"},
                    token="owner-token",
                    headers={"Idempotency-Key": "http-idem"},
                )
                self.assertEqual(note_id, replay["id"])
                with self.assertRaises(urllib.error.HTTPError) as bearer_none:
                    request_json(base + "/v1/memory/search?q=x", token="None")
                self.assertEqual(401, bearer_none.exception.code)
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    request_json(
                        base + "/v1/memory/notes",
                        {"type": "note", "title": "share", "visibility": "shared", "body": "no", "idempotencyKey": "denied"},
                        token="writer-token",
                    )
                self.assertEqual(403, denied.exception.code)
                denied_body = json.loads(denied.exception.read().decode("utf-8"))
                self.assertEqual({"error", "traceId"}, set(denied_body))
                self.assertEqual({"code", "message", "retryable"}, set(denied_body["error"]))
                self.assertEqual("SCOPE_DENIED", denied_body["error"]["code"])
                self.assertNotIn(str(tmp), json.dumps(denied_body))

                boundary = request_json(
                    base + "/v1/memory/notes",
                    {"type": "note", "title": "boundary", "visibility": "private", "body": "x" * 262_144, "idempotencyKey": "boundary"},
                    token="owner-token",
                )
                self.assertIn("id", boundary)
                with self.assertRaises(urllib.error.HTTPError) as too_large:
                    request_json(
                        base + "/v1/memory/notes",
                        {"type": "note", "title": "too large", "visibility": "private", "body": "x" * 262_145, "idempotencyKey": "too-large"},
                        token="owner-token",
                    )
                self.assertEqual(413, too_large.exception.code)
                too_large_body = json.loads(too_large.exception.read().decode("utf-8"))
                self.assertEqual("REQUEST_TOO_LARGE", too_large_body["error"]["code"])

                prefix, suffix = b'{"padding":"', b'"}'
                exact_raw = prefix + (b"x" * (1_048_576 - len(prefix) - len(suffix))) + suffix
                exact_request = urllib.request.Request(
                    base + "/v1/memory/notes",
                    data=exact_raw,
                    headers={"Content-Type": "application/json", "Authorization": "Bearer owner-token"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as exact_boundary:
                    urllib.request.urlopen(exact_request, timeout=10)
                self.assertEqual(400, exact_boundary.exception.code)
                over_raw = prefix + (b"x" * (1_048_577 - len(prefix) - len(suffix))) + suffix
                over_request = urllib.request.Request(
                    base + "/v1/memory/notes",
                    data=over_raw,
                    headers={"Content-Type": "application/json", "Authorization": "Bearer owner-token"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as over_boundary:
                    urllib.request.urlopen(over_request, timeout=10)
                self.assertEqual(413, over_boundary.exception.code)
                self.assertEqual("REQUEST_TOO_LARGE", json.loads(over_boundary.exception.read().decode("utf-8"))["error"]["code"])
            finally:
                httpd.shutdown()
                thread.join(timeout=5)
                httpd.server_close()

    def test_offline_delivery_restart_fetch_ack_and_private_poison(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            principals = {
                "owner-token": {"subject": "agent.alpha", "kind": "agent", "tokenId": "tok_owner", "scopes": ["memory.read", "memory.write", "memory.share"]},
                "beta-token": {"subject": "agent.beta", "kind": "agent", "tokenId": "tok_beta", "scopes": ["memory.read"]},
                "admin-token": {"subject": "local.operator", "kind": "operator", "tokenId": "tok_admin", "scopes": ["memory.read", "memory.admin"]},
            }
            first = make_server(tmp, port=0, enable_memory=True, enable_delivery=True, principals=principals)
            first_thread = threading.Thread(target=first.serve_forever, daemon=True)
            first_thread.start()
            first_base = f"http://127.0.0.1:{first.server_port}"
            try:
                capabilities = request_json(first_base + "/v1/capabilities", token="owner-token")
                self.assertTrue(capabilities["memorySharing"])
                self.assertTrue(capabilities["timelineGraph"])
                self.assertTrue(capabilities["safeWakeup"])
                self.assertFalse(capabilities["taskLog"])
                self.assertFalse(capabilities["memoryFull"])
                shared = request_json(
                    first_base + "/v1/memory/notes",
                    {"type": "observation", "title": "offline", "visibility": "shared", "about": ["agent.beta"], "body": "offline shared"},
                    token="owner-token", headers={"Idempotency-Key": "offline-shared"},
                )
                replay = request_json(
                    first_base + "/v1/memory/notes",
                    {"type": "observation", "title": "offline", "visibility": "shared", "about": ["agent.beta"], "body": "offline shared"},
                    token="owner-token", headers={"Idempotency-Key": "offline-shared"},
                )
                self.assertEqual(shared["traceId"], replay["traceId"])
                request_json(
                    first_base + "/v1/memory/notes",
                    {"type": "observation", "title": "private poison", "visibility": "private", "about": ["agent.beta"], "body": "must never leak"},
                    token="owner-token", headers={"Idempotency-Key": "offline-private"},
                )
            finally:
                first.shutdown()
                first_thread.join(timeout=5)
                first.server_close()

            second = make_server(tmp, port=0, enable_memory=True, enable_delivery=True, principals=principals)
            second_thread = threading.Thread(target=second.serve_forever, daemon=True)
            second_thread.start()
            second_base = f"http://127.0.0.1:{second.server_port}"
            try:
                fetched = request_json(second_base + "/v1/memory/inbox?consumerId=desktop.a", token="beta-token")
                self.assertEqual([shared["id"]], [item["note"]["id"] for item in fetched["items"]])
                self.assertEqual(shared["id"], fetched["items"][0]["provenance"]["noteId"])
                self.assertNotIn("must never leak", json.dumps(fetched))
                with self.assertRaises(urllib.error.HTTPError) as forged:
                    request_json(
                        second_base + "/v1/memory/inbox/ack",
                        {"consumerId": "desktop.a", "cursor": fetched["cursor"] + "x"},
                        token="beta-token",
                    )
                self.assertEqual("CURSOR_INVALID", json.loads(forged.exception.read().decode("utf-8"))["error"]["code"])
                request_json(
                    second_base + "/v1/memory/inbox/ack",
                    {"consumerId": "desktop.a", "cursor": fetched["cursor"]},
                    token="beta-token",
                )
                after = request_json(second_base + "/v1/memory/inbox?consumerId=desktop.a", token="beta-token")
                self.assertEqual([], after["items"])
                receipts = request_json(second_base + f"/v1/memory/receipts?traceId={shared['traceId']}", token="admin-token")
                self.assertEqual({"write", "index", "delivery", "ack"}, {item["phase"] for item in receipts["items"]})
                self.assertNotIn("offline shared", json.dumps(receipts))
            finally:
                second.shutdown()
                second_thread.join(timeout=5)
                second.server_close()


if __name__ == "__main__":
    unittest.main()



