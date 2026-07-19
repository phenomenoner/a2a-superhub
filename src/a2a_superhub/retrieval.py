from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable


DENSE_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DENSE_SOURCE = "qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"
DENSE_REVISION = "faf4aa4225822f3bc6376869cb1164e8e3feedd0"
DENSE_LICENSE = "apache-2.0"
DENSE_DIMENSION = 384
DENSE_TOKENIZER_CONFIG_HASH = "63f894a4ce2e6b4a610a1b9b33667a27ebce95eef77bb02ad0e32a5147933d4c"
SPARSE_MODEL = "Qdrant/bm25"
SPARSE_REVISION = "e499a1f8d6bec960aab5533a0941bf914e70faf9"
SPARSE_LICENSE = "apache-2.0"


@dataclass(frozen=True)
class EmbeddingManifest:
    schema: str
    dense_model: str
    dense_source: str
    dense_revision: str
    dense_license: str
    dense_dimension: int
    sparse_model: str
    sparse_revision: str
    sparse_license: str
    chunk_chars: int
    chunk_overlap: int
    tokenizer_config_hash: str

    @classmethod
    def create(
        cls, *, dense_model: str, dense_revision: str, dense_dimension: int,
        sparse_model: str, sparse_revision: str, chunk_chars: int,
        chunk_overlap: int, dense_source: str | None = None,
        dense_license: str = "unknown", sparse_license: str = "unknown",
        tokenizer_config_hash: str | None = None,
    ) -> "EmbeddingManifest":
        config = {
            "denseModel": dense_model, "denseRevision": dense_revision,
            "sparseModel": sparse_model, "sparseRevision": sparse_revision,
            "chunkChars": chunk_chars, "chunkOverlap": chunk_overlap,
        }
        return cls(
            schema="a2a-superhub.embedding-manifest.v1",
            dense_model=dense_model,
            dense_source=dense_source or dense_model,
            dense_revision=dense_revision,
            dense_license=dense_license,
            dense_dimension=dense_dimension,
            sparse_model=sparse_model,
            sparse_revision=sparse_revision,
            sparse_license=sparse_license,
            chunk_chars=chunk_chars,
            chunk_overlap=chunk_overlap,
            tokenizer_config_hash=tokenizer_config_hash or hashlib.sha256(
                json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        )

    def compatible_with(self, other: "EmbeddingManifest") -> bool:
        return self == other

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:16]


def deterministic_chunks(
    note: dict[str, Any], *, max_chars: int = 1200, overlap_chars: int = 120
) -> list[dict[str, Any]]:
    if max_chars < 32 or overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("invalid chunk size or overlap")
    metadata = "\n".join(
        str(value) for value in (
            note["id"], note.get("title", ""), note.get("author", ""),
            note.get("project", ""), " ".join(note.get("tags") or []),
            (note.get("source") or {}).get("relativePath", ""),
        ) if value
    )
    body = str(note.get("body", "")).replace("\r\n", "\n").replace("\r", "\n")
    text = f"{metadata}\n{body}".strip()
    chunks: list[dict[str, Any]] = []
    start = 0
    ordinal = 0
    while start < len(text) or (not chunks and start == 0):
        end = min(len(text), start + max_chars)
        if end < len(text):
            boundary = max(text.rfind("\n", start + max_chars // 2, end), text.rfind(" ", start + max_chars // 2, end))
            if boundary > start:
                end = boundary
        content = text[start:end].strip()
        digest = hashlib.sha256(
            f"{note['id']}\0{ordinal}\0{content}".encode("utf-8")
        ).hexdigest()[:24]
        chunks.append({
            "chunkId": f"chk_{digest}", "noteId": note["id"],
            "ordinal": ordinal, "text": content,
        })
        if end >= len(text):
            break
        start = max(start + 1, end - overlap_chars)
        ordinal += 1
    return chunks


def evaluate_rankings(
    rankings: dict[str, list[str]], judgments: dict[str, dict[str, int]]
) -> dict[str, float]:
    recalls: list[float] = []
    ndcgs: list[float] = []
    for query_id, relevance in judgments.items():
        ranking = rankings.get(query_id, [])
        relevant = set(relevance)
        recalls.append(len(set(ranking[:5]) & relevant) / len(relevant) if relevant else 1.0)
        dcg = sum(
            (2 ** relevance.get(note_id, 0) - 1) / math.log2(index + 2)
            for index, note_id in enumerate(ranking[:10])
        )
        ideal = sorted(relevance.values(), reverse=True)[:10]
        idcg = sum((2**grade - 1) / math.log2(index + 2) for index, grade in enumerate(ideal))
        ndcgs.append(dcg / idcg if idcg else 1.0)
    return {
        "recallAt5": sum(recalls) / len(recalls) if recalls else 1.0,
        "ndcgAt10": sum(ndcgs) / len(ndcgs) if ndcgs else 1.0,
    }


def enforce_thresholds(metrics: dict[str, float], thresholds: dict[str, Any]) -> None:
    quality = thresholds["quality"]
    checks = {
        "hybridRecallAt5": (metrics.get("hybridRecallAt5", metrics.get("recallAt5", 0)), quality["hybridRecallAt5Min"], ">="),
        "hybridNdcgAt10": (metrics.get("hybridNdcgAt10", metrics.get("ndcgAt10", 0)), quality["hybridNdcgAt10Min"], ">="),
        "unauthorizedResults": (metrics.get("unauthorizedResults", 0), quality["unauthorizedResultsMax"], "<="),
    }
    failed = [name for name, (actual, expected, op) in checks.items() if (actual < expected if op == ">=" else actual > expected)]
    if failed:
        raise ValueError("retrieval thresholds failed: " + ", ".join(failed))


def local_server_switch_reasons(metrics: dict[str, float], thresholds: dict[str, Any]) -> list[str]:
    gate = thresholds["switchGate"]["serverRequiredWhenAny"]
    mapping = {
        "warmQueryP95MsAbove": "warmQueryP95Ms",
        "buildSecondsAbove": "buildSeconds",
        "processRssBytesAbove": "processRssBytes",
        "derivedIndexBytesAbove": "derivedIndexBytes",
    }
    return [reason for reason, metric in mapping.items() if metrics.get(metric, 0) > gate[reason]]


class QdrantRetrievalProvider:
    """Derived, burnable hybrid index. Authoritative Markdown remains elsewhere."""

    def __init__(
        self, state_dir: str | Path, *, mode: str = "local", url: str | None = None,
        cache_dir: str | Path | None = None, chunk_chars: int = 1200,
        chunk_overlap: int = 120,
    ):
        if mode not in {"local", "server"}:
            raise ValueError("retrieval mode must be local or server")
        if mode == "server" and not url:
            raise ValueError("server retrieval mode requires an explicit URL")
        self.root = Path(state_dir) / "retrieval"
        self.root.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.url = url
        self.cache_dir = Path(cache_dir) if cache_dir else self.root / "models"
        self.chunk_chars = chunk_chars
        self.chunk_overlap = chunk_overlap
        self.manifest = EmbeddingManifest.create(
            dense_model=DENSE_MODEL, dense_source=DENSE_SOURCE,
            dense_revision=DENSE_REVISION, dense_license=DENSE_LICENSE,
            dense_dimension=DENSE_DIMENSION,
            sparse_model=SPARSE_MODEL, sparse_revision=SPARSE_REVISION,
            sparse_license=SPARSE_LICENSE,
            chunk_chars=chunk_chars, chunk_overlap=chunk_overlap,
            tokenizer_config_hash=DENSE_TOKENIZER_CONFIG_HASH,
        )
        self.active_path = self.root / "active.json"
        self.resume_path = self.root / "rebuild.json"
        self.last_fallback_reason: str | None = None
        self._dense = None
        self._sparse = None

    def _dependencies(self):
        try:
            from fastembed import SparseTextEmbedding, TextEmbedding
            from qdrant_client import QdrantClient, models
        except ImportError as exc:
            raise RuntimeError("hybrid retrieval requires the 'search' extra") from exc
        return TextEmbedding, SparseTextEmbedding, QdrantClient, models

    def _models(self):
        TextEmbedding, SparseTextEmbedding, _, _ = self._dependencies()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if self._dense is None:
            self._dense = TextEmbedding(model_name=DENSE_MODEL, cache_dir=str(self.cache_dir))
        if self._sparse is None:
            self._sparse = SparseTextEmbedding(model_name=SPARSE_MODEL, cache_dir=str(self.cache_dir))
        self._verify_cache_revision(DENSE_SOURCE, DENSE_REVISION, DENSE_TOKENIZER_CONFIG_HASH)
        self._verify_cache_revision(SPARSE_MODEL, SPARSE_REVISION)
        return self._dense, self._sparse

    def _verify_cache_revision(self, source: str, expected: str, config_hash: str | None = None) -> None:
        repository = self.cache_dir / ("models--" + source.replace("/", "--"))
        ref = repository / "refs" / "main"
        snapshots = repository / "snapshots"
        actual = ref.read_text(encoding="utf-8").strip() if ref.is_file() else None
        if actual != expected or not (snapshots / expected).is_dir():
            raise RuntimeError(
                f"embedding cache revision mismatch for {source}; "
                "pre-provision the pinned revision or update the product manifest"
            )
        if config_hash is not None:
            digest = hashlib.sha256()
            snapshot = snapshots / expected
            for name in ("config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json"):
                path = snapshot / name
                if path.is_file():
                    digest.update(name.encode())
                    digest.update(b"\0")
                    digest.update(path.read_bytes())
                    digest.update(b"\0")
            if digest.hexdigest() != config_hash:
                raise RuntimeError(f"embedding tokenizer/config hash mismatch for {source}")

    def _client(self):
        _, _, QdrantClient, _ = self._dependencies()
        if self.mode == "server":
            return QdrantClient(url=self.url, timeout=30)
        return QdrantClient(path=str(self.root / "qdrant"))

    def capabilities(self) -> dict[str, Any]:
        version = "embedded"
        available = True
        if self.mode == "server":
            import urllib.request
            try:
                with urllib.request.urlopen(self.url.rstrip("/") + "/", timeout=5) as response:
                    version = json.load(response)["version"]
            except (OSError, ValueError, KeyError):
                version = "unavailable"
                available = False
        parts = tuple(int(part) for part in re.findall(r"\d+", version)[:3]) if version != "embedded" else ()
        return {
            "mode": self.mode, "version": version, "available": available,
            "dense": available, "sparse": available,
            "rrf": available, "parameterizedRrf": available and (self.mode == "local" or parts >= (1, 16)),
            "weightedRrf": available and (self.mode == "local" or parts >= (1, 17)),
            "recency": "client-rerank", "filterPushdown": True,
        }

    @staticmethod
    def _point_id(chunk_id: str) -> int:
        return int(hashlib.sha256(chunk_id.encode()).hexdigest()[:15], 16)

    def _atomic_json(self, path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def rebuild(
        self, notes: Iterable[dict[str, Any]], *, source_revision: Callable[[str], int] | None = None,
        fail_after_chunks: int | None = None,
    ) -> dict[str, Any]:
        _, _, _, models = self._dependencies()
        dense_model, sparse_model = self._models()
        notes = list(notes)
        corpus_fingerprint = hashlib.sha256(json.dumps(
            notes, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()).hexdigest()[:12]
        namespace = hashlib.sha256(str(Path(self.root).resolve()).encode("utf-8")).hexdigest()[:10]
        collection = f"memory_{namespace}_{self.manifest.fingerprint}_{corpus_fingerprint}"
        client = self._client()
        indexed = 0
        resumed = False
        try:
            if not client.collection_exists(collection):
                client.create_collection(
                    collection_name=collection,
                    vectors_config={"dense": models.VectorParams(size=DENSE_DIMENSION, distance=models.Distance.COSINE)},
                    sparse_vectors_config={"sparse": models.SparseVectorParams(modifier=models.Modifier.IDF)},
                )
            else:
                resumed = self.resume_path.exists()
            self._atomic_json(self.resume_path, {"schema": "a2a-superhub.rebuild-state.v1", "collection": collection, "complete": False})
            for note in notes:
                for chunk in deterministic_chunks(note, max_chars=self.chunk_chars, overlap_chars=self.chunk_overlap):
                    point_id = self._point_id(chunk["chunkId"])
                    existing = client.retrieve(collection, ids=[point_id], with_payload=False)
                    if existing:
                        continue
                    dense = next(dense_model.embed([chunk["text"]])).tolist()
                    sparse = next(sparse_model.embed([chunk["text"]]))
                    payload = {
                        "noteId": note["id"], "chunkId": chunk["chunkId"], "text": chunk["text"],
                        "author": note["author"], "visibility": note["visibility"],
                        "recordedEpoch": datetime.fromisoformat(note["recordedAt"].replace("Z", "+00:00")).timestamp(),
                        "contentHash": hashlib.sha256(json.dumps(note, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                        "sourceRevision": source_revision(note["id"]) if source_revision else 0,
                    }
                    client.upsert(collection, points=[models.PointStruct(
                        id=point_id,
                        vector={"dense": dense, "sparse": models.SparseVector(indices=sparse.indices.tolist(), values=sparse.values.tolist())},
                        payload=payload,
                    )], wait=True)
                    indexed += 1
                    if fail_after_chunks is not None and indexed >= fail_after_chunks:
                        raise RuntimeError("injected retrieval rebuild interruption")
            previous = json.loads(self.active_path.read_text()) if self.active_path.exists() else None
            active = {"schema": "a2a-superhub.active-index.v1", "collection": collection, "manifest": asdict(self.manifest), "previous": previous and previous.get("collection")}
            self._atomic_json(self.active_path, active)
            self._atomic_json(self.resume_path, {"schema": "a2a-superhub.rebuild-state.v1", "collection": collection, "complete": True})
            return {"collection": collection, "indexedChunks": indexed, "notes": len(notes), "resumed": resumed, "previousCollection": active["previous"]}
        finally:
            client.close()

    @staticmethod
    def _auth_filter(principal: Any, models: Any):
        if principal.has("memory.admin"):
            return None
        return models.Filter(min_should=models.MinShould(conditions=[
            models.FieldCondition(key="visibility", match=models.MatchValue(value="shared")),
            models.FieldCondition(key="author", match=models.MatchValue(value=principal.subject)),
            models.FieldCondition(key="visibility", match=models.MatchValue(value=f"direct:{principal.subject}")),
        ], min_count=1))

    def search(
        self, query: str, principal: Any, *, load_note: Callable[[str], dict[str, Any]],
        can_read: Callable[[Any, dict[str, Any]], bool], limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not self.active_path.exists():
            raise RuntimeError("hybrid index has not been built")
        _, _, _, models = self._dependencies()
        dense_model, sparse_model = self._models()
        active = json.loads(self.active_path.read_text(encoding="utf-8"))
        if active.get("manifest") != asdict(self.manifest):
            raise RuntimeError("embedding manifest changed; reindex required")
        dense = next(dense_model.query_embed(query)).tolist()
        sparse = next(sparse_model.query_embed(query))
        auth_filter = self._auth_filter(principal, models)
        prefetch = [
            models.Prefetch(query=dense, using="dense", filter=auth_filter, limit=max(20, limit * 3)),
            models.Prefetch(query=models.SparseVector(indices=sparse.indices.tolist(), values=sparse.values.tolist()), using="sparse", filter=auth_filter, limit=max(20, limit * 3)),
        ]
        client = self._client()
        try:
            response = client.query_points(
                collection_name=active["collection"], prefetch=prefetch,
                query=models.RrfQuery(rrf=models.Rrf(k=60)), query_filter=auth_filter,
                limit=max(20, limit * 3), with_payload=True,
            )
        finally:
            client.close()
        now = max((point.payload["recordedEpoch"] for point in response.points), default=0)
        candidates: dict[str, tuple[float, dict[str, Any]]] = {}
        for point in response.points:
            payload = point.payload
            try:
                note = load_note(payload["noteId"])
            except (KeyError, ValueError):
                continue
            current_hash = hashlib.sha256(json.dumps(note, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if current_hash != payload.get("contentHash") or not can_read(principal, note):
                continue
            age_days = max(0.0, (now - payload["recordedEpoch"]) / 86400)
            score = float(point.score) + 0.005 * math.exp(-age_days / 45.0)
            previous = candidates.get(note["id"])
            if previous is None or score > previous[0]:
                candidates[note["id"]] = (score, note)
        return [item[1] for item in sorted(candidates.values(), key=lambda item: (-item[0], item[1]["id"]))[:limit]]

    def status(self) -> dict[str, Any]:
        active = json.loads(self.active_path.read_text()) if self.active_path.exists() else None
        resume = json.loads(self.resume_path.read_text()) if self.resume_path.exists() else None
        return {"provider": "qdrant", "capabilities": self.capabilities(), "active": active, "rebuild": resume, "fallbackReason": self.last_fallback_reason}
