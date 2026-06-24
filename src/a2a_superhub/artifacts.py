from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .models import new_id, utc_now


class ArtifactStore:
    """Content-addressed artifact storage with JSON manifests."""

    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.root = self.state_dir / "artifacts"
        self.blobs = self.root / "blobs" / "sha256"
        self.manifests = self.root / "manifests"
        self.temp = self.root / "temp"

    def init(self) -> None:
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.manifests.mkdir(parents=True, exist_ok=True)
        self.temp.mkdir(parents=True, exist_ok=True)

    def put_bytes(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        media_type: str = "application/octet-stream",
        created_by: str = "unknown",
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.init()
        digest = hashlib.sha256(data).hexdigest()
        blob_dir = self.blobs / digest[:2] / digest[2:4]
        blob_dir.mkdir(parents=True, exist_ok=True)
        blob_path = blob_dir / digest
        if not blob_path.exists():
            tmp_path = self.temp / f"{new_id('blob')}.tmp"
            tmp_path.write_bytes(data)
            os.replace(tmp_path, blob_path)
        artifact_id = f"art_{digest[:32]}"
        manifest = {
            "schema": "a2a-superhub.artifact.v1",
            "artifactId": artifact_id,
            "sha256": digest,
            "sizeBytes": len(data),
            "mediaType": media_type,
            "filename": filename or artifact_id,
            "storageUri": f"hub-cas://sha256/{digest}",
            "createdBy": created_by,
            "createdAt": utc_now(),
            "policy": policy or {"rawTranscript": False, "containsSecrets": False},
        }
        manifest_path = self.manifests / f"{artifact_id}.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return manifest

    def put_base64(self, content_base64: str, **metadata: Any) -> dict[str, Any]:
        return self.put_bytes(base64.b64decode(content_base64), **metadata)

    def get_manifest(self, artifact_id: str) -> dict[str, Any] | None:
        path = self.manifests / f"{artifact_id}.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

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
            raise ValueError(f"artifact checksum mismatch for {artifact_id}")
        return data

    def list_manifests(self) -> list[dict[str, Any]]:
        self.init()
        out: list[dict[str, Any]] = []
        for path in sorted(self.manifests.glob("*.json")):
            out.append(json.loads(path.read_text(encoding="utf-8")))
        return out
