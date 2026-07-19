"""Fixed-corpus FastEmbed and Qdrant retrieval evaluation.

This is deliberately an evaluation harness, not the production retrieval
provider.  It reads the already-frozen corpus and thresholds and exits nonzero
when the candidate does not satisfy the entry gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from fastembed import SparseTextEmbedding, TextEmbedding
from qdrant_client import QdrantClient, models


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "retrieval" / "fixtures"
DENSE_CANDIDATES = (
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "jinaai/jina-embeddings-v2-base-zh",
)
SPARSE_MODEL = "Qdrant/bm25"
COLLECTION = "retrieval_entry_evaluation"


def _read_json(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _model_info(model_name: str, *, sparse: bool = False) -> dict[str, Any]:
    inventory = (
        SparseTextEmbedding.list_supported_models()
        if sparse
        else TextEmbedding.list_supported_models()
    )
    return next(dict(item) for item in inventory if item["model"] == model_name)


def _document(note: dict[str, Any]) -> str:
    source = note.get("source") or {}
    fields = [
        note["id"], note.get("title", ""), note.get("body", ""),
        note.get("author", ""), note.get("visibility", ""),
        note.get("project", ""), source.get("relativePath", ""),
        source.get("taskId", ""), " ".join(note.get("participants") or []),
        " ".join(note.get("about") or []), " ".join(note.get("tags") or []),
    ]
    return "\n".join(str(value) for value in fields if value)


def _visible(note: dict[str, Any], principal: str) -> bool:
    visibility = note.get("visibility")
    return (
        visibility == "shared"
        or note.get("author") == principal
        or visibility == f"direct:{principal}"
    )


def _visibility_filter(principal: str) -> models.Filter:
    return models.Filter(
        min_should=models.MinShould(
            conditions=[
                models.FieldCondition(key="visibility", match=models.MatchValue(value="shared")),
                models.FieldCondition(key="author", match=models.MatchValue(value=principal)),
                models.FieldCondition(
                    key="visibility", match=models.MatchValue(value=f"direct:{principal}")
                ),
            ],
            min_count=1,
        )
    )


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def _metrics(
    rankings: dict[str, list[str]], corpus: dict[str, Any]
) -> dict[str, float]:
    recalls: list[float] = []
    ndcgs: list[float] = []
    by_id = {query["id"]: query for query in corpus["queries"]}
    for query_id, ranked in rankings.items():
        relevance = by_id[query_id]["relevance"]
        relevant = set(relevance)
        recalls.append(len(set(ranked[:5]) & relevant) / len(relevant))
        dcg = sum(
            (2 ** relevance.get(note_id, 0) - 1) / math.log2(rank + 2)
            for rank, note_id in enumerate(ranked[:10])
        )
        ideal = sorted(relevance.values(), reverse=True)
        idcg = sum((2**grade - 1) / math.log2(rank + 2) for rank, grade in enumerate(ideal[:10]))
        ndcgs.append(dcg / idcg if idcg else 0.0)
    return {
        "recallAt5": statistics.fmean(recalls),
        "ndcgAt10": statistics.fmean(ndcgs),
    }


def _fts_rankings(corpus: dict[str, Any]) -> dict[str, list[str]]:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE notes_fts USING fts5(note_id UNINDEXED, title, body)")
    conn.executemany(
        "INSERT INTO notes_fts(note_id,title,body) VALUES (?,?,?)",
        [(n["id"], n["title"], n["body"]) for n in corpus["notes"]],
    )
    notes = {note["id"]: note for note in corpus["notes"]}
    rankings: dict[str, list[str]] = {}
    for query in corpus["queries"]:
        terms = re.findall(r"[^\W_]+", query["text"], flags=re.UNICODE)
        expression = " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
        try:
            rows = conn.execute(
                "SELECT note_id FROM notes_fts WHERE notes_fts MATCH ? ORDER BY rank, note_id LIMIT 20",
                (expression,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        rankings[query["id"]] = [
            row[0] for row in rows if _visible(notes[row[0]], query["principal"])
        ][:10]
    conn.close()
    return rankings


def _cache_snapshot(cache_dir: Path) -> dict[str, Any]:
    files = [path for path in cache_dir.rglob("*") if path.is_file()]
    refs: dict[str, str] = {}
    for path in files:
        if path.parent.name == "refs":
            try:
                refs[str(path.relative_to(cache_dir))] = path.read_text(encoding="utf-8").strip()
            except UnicodeDecodeError:
                pass
    return {
        "bytes": sum(path.stat().st_size for path in files),
        "fileCount": len(files),
        "revisions": refs,
    }


def _rss_bytes() -> int | None:
    if platform.system() == "Windows":
        try:
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                ctypes.windll.kernel32.GetCurrentProcess(),
                ctypes.byref(counters), counters.cb,
            )
            return int(counters.WorkingSetSize) if ok else None
        except (AttributeError, OSError):
            return None
    try:
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(value if platform.system() == "Darwin" else value * 1024)
    except (ImportError, OSError):
        return None


def _point_payload(note: dict[str, Any]) -> dict[str, Any]:
    return {
        "noteId": note["id"],
        "author": note["author"],
        "visibility": note["visibility"],
        "recordedAt": note["recordedAt"],
        "recordedEpoch": datetime.fromisoformat(note["recordedAt"].replace("Z", "+00:00")).timestamp(),
    }


def _query(
    client: QdrantClient,
    dense_model: TextEmbedding,
    sparse_model: SparseTextEmbedding,
    query: dict[str, Any],
) -> list[dict[str, Any]]:
    dense = next(dense_model.query_embed(query["text"])).tolist()
    sparse = next(sparse_model.query_embed(query["text"]))
    auth_filter = _visibility_filter(query["principal"])
    response = client.query_points(
        collection_name=COLLECTION,
        prefetch=[
            models.Prefetch(query=dense, using="dense", filter=auth_filter, limit=20),
            models.Prefetch(
                query=models.SparseVector(
                    indices=sparse.indices.tolist(), values=sparse.values.tolist()
                ),
                using="sparse", filter=auth_filter, limit=20,
            ),
        ],
        query=models.RrfQuery(rrf=models.Rrf(k=60)),
        query_filter=auth_filter,
        limit=10,
        with_payload=True,
    )
    now = max(point.payload["recordedEpoch"] for point in response.points) if response.points else 0
    rescored = []
    for point in response.points:
        age_days = max(0.0, (now - point.payload["recordedEpoch"]) / 86400.0)
        recency = 0.005 * math.exp(-age_days / 45.0)
        rescored.append((float(point.score) + recency, point))
    rescored.sort(key=lambda item: (-item[0], item[1].payload["noteId"]))
    return [
        {"noteId": point.payload["noteId"], "score": score}
        for score, point in rescored
    ]


def _create_collection(client: QdrantClient, dimension: int) -> None:
    if client.collection_exists(COLLECTION):
        client.delete_collection(COLLECTION)
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config={
            "dense": models.VectorParams(size=dimension, distance=models.Distance.COSINE)
        },
        sparse_vectors_config={
            "sparse": models.SparseVectorParams(modifier=models.Modifier.IDF)
        },
    )


def _evaluate_candidate(
    model_name: str,
    corpus: dict[str, Any],
    cache_dir: Path,
    location: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    dense_started = time.perf_counter()
    dense_model = TextEmbedding(model_name=model_name, cache_dir=str(cache_dir))
    dense_init_ms = (time.perf_counter() - dense_started) * 1000
    sparse_started = time.perf_counter()
    sparse_model = SparseTextEmbedding(model_name=SPARSE_MODEL, cache_dir=str(cache_dir))
    sparse_init_ms = (time.perf_counter() - sparse_started) * 1000
    documents = [_document(note) for note in corpus["notes"]]
    embed_started = time.perf_counter()
    dense_vectors = [vector.tolist() for vector in dense_model.embed(documents)]
    sparse_vectors = list(sparse_model.embed(documents))
    embed_seconds = time.perf_counter() - embed_started
    dimension = len(dense_vectors[0])

    local_path: Path | None = None
    if location == "local":
        local_path = Path(tempfile.mkdtemp(prefix="a2a-superhub-retrieval-qdrant-local-"))
        client = QdrantClient(path=str(local_path))
    else:
        client = QdrantClient(url=location, timeout=30)
    try:
        build_started = time.perf_counter()
        _create_collection(client, dimension)
        client.upsert(
            collection_name=COLLECTION,
            wait=True,
            points=[
                models.PointStruct(
                    id=index,
                    vector={
                        "dense": dense_vectors[index],
                        "sparse": models.SparseVector(
                            indices=sparse_vectors[index].indices.tolist(),
                            values=sparse_vectors[index].values.tolist(),
                        ),
                    },
                    payload=_point_payload(note),
                )
                for index, note in enumerate(corpus["notes"])
            ],
        )
        build_seconds = time.perf_counter() - build_started

        latencies: list[float] = []
        rankings: dict[str, list[str]] = {}
        details: dict[str, list[dict[str, Any]]] = {}
        for query in corpus["queries"]:
            query_started = time.perf_counter()
            results = _query(client, dense_model, sparse_model, query)
            latencies.append((time.perf_counter() - query_started) * 1000)
            rankings[query["id"]] = [item["noteId"] for item in results]
            details[query["id"]] = results
        warm_latencies: list[float] = []
        for _ in range(2):
            for query in corpus["queries"]:
                query_started = time.perf_counter()
                _query(client, dense_model, sparse_model, query)
                warm_latencies.append((time.perf_counter() - query_started) * 1000)

        notes = {note["id"]: note for note in corpus["notes"]}
        unauthorized_results = 0
        unauthorized_scores_or_snippets = 0
        hidden_observations: list[dict[str, str]] = []
        for query in corpus["queries"]:
            returned = set(rankings[query["id"]])
            for note_id in returned:
                if not _visible(notes[note_id], query["principal"]):
                    unauthorized_results += 1
            for hidden in query.get("expectedHidden", []):
                if hidden in returned:
                    unauthorized_scores_or_snippets += 1
                    hidden_observations.append({"queryId": query["id"], "noteId": hidden})
        result = {
            "model": model_name,
            "modelInventory": _model_info(model_name),
            "sparseModelInventory": _model_info(SPARSE_MODEL, sparse=True),
            "dimension": dimension,
            "location": location,
            "metrics": _metrics(rankings, corpus),
            "unauthorizedResults": unauthorized_results,
            "unauthorizedScoresOrSnippets": unauthorized_scores_or_snippets,
            "hiddenObservations": hidden_observations,
            "timing": {
                "denseInitMs": dense_init_ms,
                "sparseInitMs": sparse_init_ms,
                "embedCorpusSeconds": embed_seconds,
                "buildSeconds": build_seconds,
                "coldQueryP95Ms": _percentile(latencies, 0.95),
                "warmQueryP50Ms": _percentile(warm_latencies, 0.50),
                "warmQueryP95Ms": _percentile(warm_latencies, 0.95),
                "totalSeconds": time.perf_counter() - started,
            },
            "rssBytes": _rss_bytes(),
            "rankings": rankings,
            "rankingDetails": details,
        }
        if local_path is not None:
            result["derivedIndexBytes"] = sum(
                path.stat().st_size for path in local_path.rglob("*") if path.is_file()
            )
        return result
    finally:
        client.close()
        if local_path is not None:
            shutil.rmtree(local_path)


def _gate(result: dict[str, Any], baseline: dict[str, float], thresholds: dict[str, Any]) -> list[str]:
    quality = thresholds["quality"]
    latency = thresholds["latency"]
    failures: list[str] = []
    checks = (
        (result["metrics"]["recallAt5"] >= quality["hybridRecallAt5Min"], "hybridRecallAt5"),
        (result["metrics"]["ndcgAt10"] >= quality["hybridNdcgAt10Min"], "hybridNdcgAt10"),
        (
            result["metrics"]["recallAt5"] - baseline["recallAt5"]
            >= quality["paraphraseRecallAt5GainOverFtsMin"],
            "recallAt5GainOverFts",
        ),
        (result["unauthorizedResults"] <= quality["unauthorizedResultsMax"], "unauthorizedResults"),
        (
            result["unauthorizedScoresOrSnippets"]
            <= quality["unauthorizedScoresOrSnippetsMax"],
            "unauthorizedScoresOrSnippets",
        ),
        (result["timing"]["coldQueryP95Ms"] <= latency["entryCorpusColdQueryP95MsMax"], "coldQueryP95Ms"),
        (result["timing"]["warmQueryP95Ms"] <= latency["entryCorpusWarmQueryP95MsMax"], "warmQueryP95Ms"),
        (result["timing"]["buildSeconds"] <= latency["entryCorpusBuildSecondsMax"], "buildSeconds"),
    )
    failures.extend(name for passed, name in checks if not passed)
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--location", default="local", help="local or an isolated Qdrant URL")
    parser.add_argument("--model", action="append", choices=DENSE_CANDIDATES)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    corpus = _read_json("retrieval-eval-corpus.json")
    thresholds = _read_json("retrieval-eval-thresholds.json")
    if not thresholds.get("frozenBeforeResults"):
        raise RuntimeError("thresholds are not frozen before results")
    cache_dir = args.cache_dir or Path(tempfile.gettempdir()) / "a2a-superhub-retrieval-fastembed-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    baseline_rankings = _fts_rankings(corpus)
    baseline = _metrics(baseline_rankings, corpus)
    models_to_run = args.model or list(DENSE_CANDIDATES)
    results = []
    for model_name in models_to_run:
        candidate = _evaluate_candidate(model_name, corpus, cache_dir, args.location)
        candidate["gateFailures"] = _gate(candidate, baseline, thresholds)
        candidate["passedEntryGate"] = not candidate["gateFailures"]
        results.append(candidate)
    payload = {
        "schema": "a2a-superhub.retrieval-entry-spike.v1",
        "corpusSha256": hashlib.sha256((FIXTURES / "retrieval-eval-corpus.json").read_bytes()).hexdigest(),
        "thresholdsSha256": hashlib.sha256((FIXTURES / "retrieval-eval-thresholds.json").read_bytes()).hexdigest(),
        "thresholdsFrozenAt": thresholds["frozenAt"],
        "baseline": baseline,
        "cache": _cache_snapshot(cache_dir),
        "platform": {"system": platform.system(), "release": platform.release(), "python": sys.version},
        "results": results,
    }
    rendered = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0 if all(result["passedEntryGate"] for result in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
