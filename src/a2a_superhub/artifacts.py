from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any, BinaryIO, Iterable

from .auth import Principal
from .models import new_id, utc_now


SHA256 = re.compile(r"^[0-9a-f]{64}$")
ARTIFACT_ID = re.compile(r"^art_[0-9a-f]{32}$")
UPLOAD_ID = re.compile(r"^upl_[0-9a-f]{32}$")
VISIBILITY = re.compile(r"^(?:private|shared|direct:[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?)$")
DEFAULT_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
DEFAULT_CHUNK_BYTES = 4 * 1024 * 1024


class ArtifactError(ValueError):
    pass


class ArtifactTooLargeError(ArtifactError):
    pass


class ArtifactConflictError(ArtifactError):
    pass


class ArtifactAccessError(ArtifactError):
    pass


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{new_id('tmp')}")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


class ArtifactStore:
    """Checksum-authoritative artifact CAS with resumable upload state."""

    def __init__(self, state_dir: str | Path, *, max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES):
        self.state_dir = Path(state_dir)
        self.root = self.state_dir / "artifacts"
        self.blobs = self.root / "blobs" / "sha256"
        self.manifests = self.root / "manifests"
        self.temp = self.root / "temp"
        self.upload_sessions = self.root / "uploads"
        self.upload_chunks = self.temp / "uploads"
        self.max_artifact_bytes = max(1, int(max_artifact_bytes))
        self._lock = threading.RLock()

    def init(self) -> None:
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.manifests.mkdir(parents=True, exist_ok=True)
        self.temp.mkdir(parents=True, exist_ok=True)
        self.upload_sessions.mkdir(parents=True, exist_ok=True)
        self.upload_chunks.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _validate_visibility(visibility: str) -> str:
        if not isinstance(visibility, str) or not VISIBILITY.fullmatch(visibility):
            raise ArtifactError("artifact visibility must be private, shared, or direct:<principal>")
        return visibility

    @staticmethod
    def _validate_digest(digest: str) -> str:
        digest = str(digest).casefold()
        if not SHA256.fullmatch(digest):
            raise ArtifactError("sha256 must contain 64 lowercase hexadecimal characters")
        return digest

    @staticmethod
    def _validate_metadata(filename: str | None, media_type: str) -> tuple[str | None, str]:
        if filename is not None and (not isinstance(filename, str) or not filename or len(filename) > 255 or "\x00" in filename):
            raise ArtifactError("filename must be 1-255 characters without NUL")
        if not isinstance(media_type, str) or not media_type or len(media_type) > 255 or "\r" in media_type or "\n" in media_type:
            raise ArtifactError("media type is invalid")
        return filename, media_type

    def _iter_chunks(self, source: BinaryIO | Iterable[bytes]) -> Iterable[bytes]:
        if hasattr(source, "read"):
            while True:
                chunk = source.read(65_536)  # type: ignore[union-attr]
                if not chunk:
                    return
                yield chunk
        else:
            yield from source

    def put_stream(
        self,
        source: BinaryIO | Iterable[bytes],
        *,
        filename: str | None = None,
        media_type: str = "application/octet-stream",
        created_by: str = "unknown",
        visibility: str = "private",
        expected_sha256: str | None = None,
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.init()
        filename, media_type = self._validate_metadata(filename, media_type)
        visibility = self._validate_visibility(visibility)
        expected = self._validate_digest(expected_sha256) if expected_sha256 else None
        if not isinstance(created_by, str) or not created_by:
            raise ArtifactError("created_by is required")
        temp_path = self.temp / f"{new_id('blob')}.tmp"
        digest = hashlib.sha256()
        size = 0
        try:
            with temp_path.open("xb") as handle:
                for chunk in self._iter_chunks(source):
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise ArtifactError("artifact stream yielded a non-bytes chunk")
                    raw = bytes(chunk)
                    size += len(raw)
                    if size > self.max_artifact_bytes:
                        raise ArtifactTooLargeError(f"artifact exceeds {self.max_artifact_bytes} byte limit")
                    digest.update(raw)
                    handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            actual = digest.hexdigest()
            if expected and actual != expected:
                raise ArtifactConflictError("artifact checksum does not match X-Artifact-SHA256")
            artifact_id = f"art_{actual[:32]}"
            blob_dir = self.blobs / actual[:2] / actual[2:4]
            blob_dir.mkdir(parents=True, exist_ok=True)
            blob_path = blob_dir / actual
            manifest_path = self.manifests / f"{artifact_id}.json"
            with self._lock:
                existing = self.get_manifest(artifact_id)
                if existing and existing.get("sha256") != actual:
                    raise ArtifactConflictError("artifact identifier collision detected")
                if existing and existing.get("createdBy") != created_by:
                    raise ArtifactConflictError("artifact checksum is already owned by another principal")
                if not blob_path.exists():
                    os.replace(temp_path, blob_path)
                manifest = existing or {
                    "schema": "a2a-superhub.artifact.v1",
                    "artifactId": artifact_id,
                    "sha256": actual,
                    "sizeBytes": size,
                    "storageUri": f"hub-cas://sha256/{actual}",
                    "createdBy": created_by,
                    "createdAt": utc_now(),
                }
                if not existing:
                    manifest.update({
                        "mediaType": media_type,
                        "filename": filename or artifact_id,
                        "visibility": visibility,
                        "policy": policy or {"rawTranscript": False, "containsSecrets": False},
                    })
                    _atomic_json(manifest_path, manifest)
                return manifest
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def put_bytes(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        media_type: str = "application/octet-stream",
        created_by: str = "unknown",
        visibility: str = "private",
        expected_sha256: str | None = None,
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.put_stream(
            [data], filename=filename, media_type=media_type, created_by=created_by,
            visibility=visibility, expected_sha256=expected_sha256, policy=policy,
        )

    def put_base64(self, content_base64: str, **metadata: Any) -> dict[str, Any]:
        try:
            data = base64.b64decode(content_base64, validate=True)
        except Exception as exc:
            raise ArtifactError("contentBase64 is invalid") from exc
        return self.put_bytes(data, **metadata)

    def _session_path(self, upload_id: str) -> Path:
        if not UPLOAD_ID.fullmatch(upload_id):
            raise ArtifactError("invalid upload id")
        return self.upload_sessions / f"{upload_id}.json"

    def _read_session(self, upload_id: str) -> dict[str, Any]:
        path = self._session_path(upload_id)
        if not path.is_file():
            raise KeyError(f"upload not found: {upload_id}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactConflictError("upload session metadata is unavailable") from exc
        if value.get("uploadId") != upload_id:
            raise ArtifactConflictError("upload session metadata is inconsistent")
        return value

    def initiate_upload(
        self,
        *,
        size_bytes: int,
        sha256: str,
        chunk_size: int = DEFAULT_CHUNK_BYTES,
        filename: str | None = None,
        media_type: str = "application/octet-stream",
        created_by: str,
        visibility: str = "private",
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.init()
        size_bytes = int(size_bytes)
        chunk_size = int(chunk_size)
        if size_bytes < 0 or size_bytes > self.max_artifact_bytes:
            raise ArtifactTooLargeError(f"artifact exceeds {self.max_artifact_bytes} byte limit")
        if chunk_size < 1 or chunk_size > self.max_artifact_bytes:
            raise ArtifactError("chunkSize is outside the allowed range")
        filename, media_type = self._validate_metadata(filename, media_type)
        digest = self._validate_digest(sha256)
        visibility = self._validate_visibility(visibility)
        upload_id = new_id("upl")
        session = {
            "schema": "a2a-superhub.artifact-upload.v1",
            "uploadId": upload_id,
            "status": "pending",
            "sizeBytes": size_bytes,
            "sha256": digest,
            "chunkSize": chunk_size,
            "chunkCount": math.ceil(size_bytes / chunk_size) if size_bytes else 0,
            "filename": filename,
            "mediaType": media_type,
            "createdBy": created_by,
            "visibility": visibility,
            "policy": policy or {"rawTranscript": False, "containsSecrets": False},
            "createdAt": utc_now(),
            "updatedAt": utc_now(),
            "artifactId": None,
        }
        _atomic_json(self._session_path(upload_id), session)
        return session

    def put_chunk(self, upload_id: str, index: int, data: bytes, *, expected_sha256: str | None = None) -> dict[str, Any]:
        self.init()
        index = int(index)
        with self._lock:
            session = self._read_session(upload_id)
            if session["status"] != "pending":
                raise ArtifactConflictError(f"upload is {session['status']}")
            if index < 0 or index >= session["chunkCount"]:
                raise ArtifactError("chunk index is outside the upload range")
            expected_size = session["chunkSize"]
            if index == session["chunkCount"] - 1:
                expected_size = session["sizeBytes"] - index * session["chunkSize"]
            if len(data) != expected_size:
                raise ArtifactConflictError("chunk size does not match the session layout")
            digest = hashlib.sha256(data).hexdigest()
            if expected_sha256 and digest != self._validate_digest(expected_sha256):
                raise ArtifactConflictError("chunk checksum mismatch")
            directory = self.upload_chunks / upload_id
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{index:08d}.chunk"
            if path.exists():
                duplicate = hashlib.sha256(path.read_bytes()).hexdigest() == digest
                if not duplicate:
                    raise ArtifactConflictError("chunk index already contains different bytes")
                return {"uploadId": upload_id, "index": index, "sha256": digest, "duplicate": True}
            temp = directory / f".{index:08d}.{new_id('tmp')}"
            try:
                with temp.open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, path)
            finally:
                if temp.exists():
                    temp.unlink()
            session["updatedAt"] = utc_now()
            _atomic_json(self._session_path(upload_id), session)
            return {"uploadId": upload_id, "index": index, "sha256": digest, "duplicate": False}

    def commit_upload(self, upload_id: str) -> dict[str, Any]:
        self.init()
        with self._lock:
            session = self._read_session(upload_id)
            if session["status"] == "completed":
                manifest = self.get_manifest(session["artifactId"])
                if not manifest:
                    raise ArtifactConflictError("completed upload points to a missing artifact")
                return {"uploadId": upload_id, "status": "completed", "artifact": manifest, "replayed": True}
            if session["status"] != "pending":
                raise ArtifactConflictError(f"upload is {session['status']}")
            directory = self.upload_chunks / upload_id
            paths = [directory / f"{index:08d}.chunk" for index in range(session["chunkCount"])]
            missing = [index for index, path in enumerate(paths) if not path.is_file()]
            if missing:
                raise ArtifactConflictError(f"upload is missing chunks: {missing}")

            def chunks() -> Iterable[bytes]:
                for path in paths:
                    yield path.read_bytes()

            manifest = self.put_stream(
                chunks(), filename=session["filename"], media_type=session["mediaType"],
                created_by=session["createdBy"], visibility=session["visibility"],
                expected_sha256=session["sha256"], policy=session["policy"],
            )
            session.update({"status": "completed", "artifactId": manifest["artifactId"], "updatedAt": utc_now()})
            _atomic_json(self._session_path(upload_id), session)
            shutil.rmtree(directory, ignore_errors=True)
            return {"uploadId": upload_id, "status": "completed", "artifact": manifest, "replayed": False}

    def cancel_upload(self, upload_id: str) -> dict[str, Any]:
        self.init()
        with self._lock:
            session = self._read_session(upload_id)
            if session["status"] == "completed":
                raise ArtifactConflictError("completed upload cannot be canceled")
            session.update({"status": "canceled", "updatedAt": utc_now()})
            _atomic_json(self._session_path(upload_id), session)
            shutil.rmtree(self.upload_chunks / upload_id, ignore_errors=True)
            return session

    def get_upload(self, upload_id: str) -> dict[str, Any]:
        return self._read_session(upload_id)

    def get_manifest(self, artifact_id: str) -> dict[str, Any] | None:
        if not ARTIFACT_ID.fullmatch(artifact_id):
            return None
        path = self.manifests / f"{artifact_id}.json"
        if not path.is_file():
            return None
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactConflictError("artifact manifest is unavailable") from exc
        if manifest.get("artifactId") != artifact_id or manifest.get("sha256", "")[:32] != artifact_id[4:]:
            raise ArtifactConflictError("artifact manifest is inconsistent")
        return manifest

    def get_bytes(self, artifact_id: str) -> bytes | None:
        manifest = self.get_manifest(artifact_id)
        if not manifest:
            return None
        digest = manifest["sha256"]
        path = self.blobs / digest[:2] / digest[2:4] / digest
        if not path.is_file():
            return None
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ArtifactConflictError(f"artifact checksum mismatch for {artifact_id}")
        return data

    @staticmethod
    def can_read(manifest: dict[str, Any], principal: Principal) -> bool:
        if not principal.has("artifact.read"):
            return False
        if principal.has("hub.admin") or principal.has("artifact.admin") or manifest.get("createdBy") == principal.subject:
            return True
        visibility = manifest.get("visibility", "private")
        return visibility == "shared" or visibility == f"direct:{principal.subject}"

    def require_read(self, artifact_id: str, principal: Principal) -> dict[str, Any]:
        manifest = self.get_manifest(artifact_id)
        if not manifest or not self.can_read(manifest, principal):
            raise KeyError(f"artifact not found: {artifact_id}")
        return manifest

    def set_visibility(self, artifact_id: str, visibility: str, principal: Principal) -> dict[str, Any]:
        visibility = self._validate_visibility(visibility)
        with self._lock:
            manifest = self.get_manifest(artifact_id)
            if not manifest:
                raise KeyError(f"artifact not found: {artifact_id}")
            if not (principal.has("hub.admin") or principal.has("artifact.admin") or (
                principal.has("artifact.write") and manifest.get("createdBy") == principal.subject
            )):
                raise ArtifactAccessError("artifact owner or admin authority is required")
            if visibility != "private" and not (principal.has("artifact.share") or principal.has("hub.admin") or principal.has("artifact.admin")):
                raise ArtifactAccessError("artifact.share scope is required")
            manifest["visibility"] = visibility
            manifest["policyRevision"] = int(manifest.get("policyRevision", 0)) + 1
            manifest["updatedAt"] = utc_now()
            _atomic_json(self.manifests / f"{artifact_id}.json", manifest)
            return manifest

    def list_manifests(self, principal: Principal | None = None) -> list[dict[str, Any]]:
        self.init()
        out: list[dict[str, Any]] = []
        for path in sorted(self.manifests.glob("*.json")):
            manifest = self.get_manifest(path.stem)
            if manifest and (principal is None or self.can_read(manifest, principal)):
                out.append(manifest)
        return out
