from __future__ import annotations

import hashlib
import tempfile
import unittest

from a2a_superhub.artifacts import (
    ArtifactAccessError,
    ArtifactConflictError,
    ArtifactStore,
    ArtifactTooLargeError,
)
from a2a_superhub.auth import Principal
from a2a_superhub.parts import normalize_a2a_parts


OWNER = Principal(
    "agent.alpha",
    "agent",
    "tok_owner",
    frozenset({"artifact.read", "artifact.write", "artifact.share"}),
)
OTHER = Principal("agent.beta", "agent", "tok_other", frozenset({"artifact.read"}))
ADMIN = Principal("local.operator", "operator", "tok_admin", frozenset({"hub.admin"}))


class ArtifactTransportTests(unittest.TestCase):
    def test_stream_upload_checks_size_checksum_and_cleans_partial_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(tmp, max_artifact_bytes=5)
            expected = hashlib.sha256(b"hello").hexdigest()
            manifest = store.put_stream(
                [b"he", b"llo"], filename="hello.txt", media_type="text/plain",
                created_by=OWNER.subject, visibility="private", expected_sha256=expected,
            )
            self.assertEqual(expected, manifest["sha256"])
            self.assertEqual(b"hello", store.get_bytes(manifest["artifactId"]))
            with self.assertRaises(ArtifactTooLargeError):
                store.put_stream([b"123", b"456"], created_by=OWNER.subject)
            with self.assertRaises(ArtifactConflictError):
                store.put_stream([b"hello"], created_by=OWNER.subject, expected_sha256="0" * 64)
            self.assertEqual([], list(store.temp.glob("*.tmp")))

    def test_chunk_upload_is_out_of_order_idempotent_restart_safe_and_cancelable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = b"abcdef"
            digest = hashlib.sha256(data).hexdigest()
            first = ArtifactStore(tmp, max_artifact_bytes=32)
            session = first.initiate_upload(
                size_bytes=len(data), chunk_size=3, sha256=digest,
                filename="letters.bin", media_type="application/octet-stream",
                created_by=OWNER.subject, visibility="private",
            )
            upload_id = session["uploadId"]
            first.put_chunk(upload_id, 1, b"def")
            duplicate = first.put_chunk(upload_id, 1, b"def")
            self.assertTrue(duplicate["duplicate"])
            with self.assertRaises(ArtifactConflictError):
                first.put_chunk(upload_id, 1, b"xyz")
            with self.assertRaises(ArtifactConflictError):
                first.commit_upload(upload_id)

            restarted = ArtifactStore(tmp, max_artifact_bytes=32)
            restarted.put_chunk(upload_id, 0, b"abc")
            result = restarted.commit_upload(upload_id)
            self.assertEqual(digest, result["artifact"]["sha256"])
            replay = restarted.commit_upload(upload_id)
            self.assertEqual(result["artifact"]["artifactId"], replay["artifact"]["artifactId"])
            self.assertFalse((restarted.upload_chunks / upload_id).exists())

            canceled = restarted.initiate_upload(
                size_bytes=3, chunk_size=3, sha256=hashlib.sha256(b"bye").hexdigest(),
                created_by=OWNER.subject,
            )
            restarted.put_chunk(canceled["uploadId"], 0, b"bye")
            status = restarted.cancel_upload(canceled["uploadId"])
            self.assertEqual("canceled", status["status"])
            self.assertFalse((restarted.upload_chunks / canceled["uploadId"]).exists())

    def test_current_manifest_controls_artifact_visibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(tmp)
            manifest = store.put_bytes(b"private", created_by=OWNER.subject, visibility="private")
            self.assertTrue(store.can_read(manifest, OWNER))
            self.assertFalse(store.can_read(manifest, OTHER))
            with self.assertRaises(ArtifactAccessError):
                store.set_visibility(manifest["artifactId"], "shared", OTHER)
            shared = store.set_visibility(manifest["artifactId"], "shared", OWNER)
            self.assertTrue(store.can_read(shared, OTHER))
            private = store.set_visibility(manifest["artifactId"], "private", ADMIN)
            self.assertFalse(store.can_read(private, OTHER))

    def test_a2a_part_mapping_preserves_official_oneof_and_legacy_is_explicit(self) -> None:
        parts = normalize_a2a_parts([
            {"text": "hello"},
            {"raw": "AAEC", "filename": "a.bin", "mediaType": "application/octet-stream"},
            {"url": "https://example.invalid/a"},
            {"data": {"answer": 42}},
        ])
        self.assertEqual(["text", "raw", "url", "data"], [part["type"] for part in parts])
        self.assertEqual(b"\x00\x01\x02", parts[1]["bytes"])
        with self.assertRaisesRegex(ValueError, "exactly one"):
            normalize_a2a_parts([{"text": "x", "data": {}}])
        legacy = normalize_a2a_parts([{"kind": "text", "text": "old"}], allow_legacy=True)
        self.assertEqual("legacy-kind", legacy[0]["mapping"])
        with self.assertRaisesRegex(ValueError, "legacy"):
            normalize_a2a_parts([{"kind": "text", "text": "old"}])


if __name__ == "__main__":
    unittest.main()
