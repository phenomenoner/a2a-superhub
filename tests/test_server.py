from __future__ import annotations

import base64
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from a2a_superhub.server import make_server


def request_json(url: str, payload: dict | None = None, token: str | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


class ServerTests(unittest.TestCase):
    def test_json_rpc_task_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            httpd = make_server(tmp, port=0)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{httpd.server_port}"
            try:
                health = request_json(base + "/healthz")
                self.assertEqual(health["status"], "ready")

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


if __name__ == "__main__":
    unittest.main()



