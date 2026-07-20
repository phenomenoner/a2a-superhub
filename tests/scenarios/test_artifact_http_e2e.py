from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from tests.scenarios.test_multimodal_derivation import make_pdf

from a2a_superhub.server import make_server


def call(
    base: str, path: str, *, token: str, method: str = "GET",
    body: bytes | dict | None = None, headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    if isinstance(body, dict):
        data = json.dumps(body).encode("utf-8")
        content_type = "application/json"
    else:
        data = body
        content_type = "application/octet-stream"
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": content_type, **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class ArtifactHttpScenarios(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        principals = {
            "owner": {
                "subject": "agent.alpha", "kind": "agent", "tokenId": "tok_owner",
                "scopes": ["artifact.read", "artifact.write", "artifact.share", "memory.read", "memory.write", "memory.share"],
            },
            "reader": {
                "subject": "agent.beta", "kind": "agent", "tokenId": "tok_reader",
                "scopes": ["artifact.read", "memory.read"],
            },
        }
        self.httpd = make_server(
            self.tmp.name, port=0, principals=principals,
            enable_memory=True, enable_derivers=True, max_artifact_bytes=1_000_000,
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()
        self.tmp.cleanup()

    def test_raw_pdf_to_search_backlink_then_current_acl_denial(self) -> None:
        pdf = make_pdf("ORCHID HTTP boundary")
        status, manifest = call(
            self.base, "/v1/artifacts/raw", token="owner", method="PUT", body=pdf,
            headers={
                "Content-Type": "application/pdf",
                "X-Artifact-Filename": "brief.pdf",
                "X-Artifact-Visibility": "shared",
                "X-Artifact-SHA256": hashlib.sha256(pdf).hexdigest(),
            },
        )
        self.assertEqual(201, status, manifest)
        artifact_id = manifest["artifactId"]
        self.assertEqual(200, call(self.base, f"/v1/artifacts/{artifact_id}", token="reader")[0])

        status, derived = call(
            self.base, f"/v1/artifacts/{artifact_id}/derive", token="owner", method="POST", body={},
        )
        self.assertEqual(201, status, derived)
        note_id = derived["noteId"]
        status, search = call(self.base, "/v1/memory/search?q=ORCHID", token="reader")
        self.assertEqual(200, status)
        self.assertEqual([note_id], [item["id"] for item in search["items"]])
        status, note = call(self.base, f"/v1/memory/notes/{note_id}", token="reader")
        self.assertEqual(200, status)
        self.assertIn(f"artifact:{artifact_id}", json.dumps(note))

        status, _ = call(
            self.base, f"/v1/artifacts/{artifact_id}/policy", token="owner", method="POST", body={"visibility": "private"},
        )
        self.assertEqual(200, status)
        self.assertEqual(404, call(self.base, f"/v1/artifacts/{artifact_id}", token="reader")[0])
        self.assertEqual([], call(self.base, "/v1/memory/search?q=ORCHID", token="reader")[1]["items"])

    def test_resumable_upload_restart_shape_and_explicit_cancel(self) -> None:
        data = b"abcdef"
        status, session = call(
            self.base, "/v1/artifacts/chunks", token="owner", method="POST",
            body={"sizeBytes": 6, "chunkSize": 3, "sha256": hashlib.sha256(data).hexdigest(), "filename": "a.bin"},
        )
        self.assertEqual(201, status)
        upload_id = session["uploadId"]
        for index, chunk in [(1, b"def"), (0, b"abc")]:
            status, result = call(
                self.base, f"/v1/artifacts/chunks/{upload_id}/{index}", token="owner", method="PUT", body=chunk,
                headers={"X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest()},
            )
            self.assertEqual(200, status, result)
        status, committed = call(
            self.base, f"/v1/artifacts/chunks/{upload_id}/commit", token="owner", method="POST", body={},
        )
        self.assertEqual(200, status, committed)
        self.assertEqual(hashlib.sha256(data).hexdigest(), committed["artifact"]["sha256"])

        _, cancel_session = call(
            self.base, "/v1/artifacts/chunks", token="owner", method="POST",
            body={"sizeBytes": 3, "chunkSize": 3, "sha256": hashlib.sha256(b"bye").hexdigest()},
        )
        status, canceled = call(
            self.base, f"/v1/artifacts/chunks/{cancel_session['uploadId']}/cancel",
            token="owner", method="POST", body={},
        )
        self.assertEqual(200, status)
        self.assertEqual("canceled", canceled["status"])

    def test_derivers_are_default_off_without_affecting_artifact_transport(self) -> None:
        self.httpd.shutdown(); self.thread.join(timeout=5); self.httpd.server_close()
        principals = {
            "owner": {
                "subject": "agent.alpha", "kind": "agent", "tokenId": "tok_owner",
                "scopes": ["artifact.read", "artifact.write", "memory.read", "memory.write"],
            },
        }
        self.httpd = make_server(self.tmp.name, port=0, principals=principals, enable_memory=True)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True); self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        status, capabilities = call(self.base, "/v1/capabilities", token="owner")
        self.assertEqual(200, status)
        self.assertFalse(capabilities["artifactDerivation"])
        status, manifest = call(
            self.base, "/v1/artifacts/raw", token="owner", method="PUT", body=b"plain",
            headers={"X-Artifact-SHA256": hashlib.sha256(b"plain").hexdigest()},
        )
        self.assertEqual(201, status)
        status, error = call(
            self.base, f"/v1/artifacts/{manifest['artifactId']}/derive", token="owner", method="POST", body={},
        )
        self.assertEqual(501, status)
        self.assertEqual("DERIVATION_NOT_ENABLED", error["error"]["code"])


if __name__ == "__main__":
    unittest.main()
