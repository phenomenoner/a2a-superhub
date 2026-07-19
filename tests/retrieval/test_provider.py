from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from a2a_superhub.auth import Principal
from a2a_superhub.memory import MemoryService
from a2a_superhub.retrieval import QdrantRetrievalProvider


class _Object:
    def __init__(self, **values):
        self.__dict__.update(values)


class _Models:
    Prefetch = RrfQuery = Rrf = SparseVector = Filter = MinShould = FieldCondition = MatchValue = _Object


class _Dense:
    def query_embed(self, value):
        yield SimpleNamespace(tolist=lambda: [0.1, 0.2])


class _Sparse:
    def query_embed(self, value):
        yield SimpleNamespace(
            indices=SimpleNamespace(tolist=lambda: [1]),
            values=SimpleNamespace(tolist=lambda: [1.0]),
        )


class _Client:
    def __init__(self, points):
        self.points = points
        self.call = None

    def query_points(self, **kwargs):
        self.call = kwargs
        return SimpleNamespace(points=self.points)

    def close(self):
        pass


class RetrievalProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.provider = QdrantRetrievalProvider(self.temp.name)
        self.provider._dependencies = lambda: (None, None, None, _Models)
        self.provider._models = lambda: (_Dense(), _Sparse())
        self.provider._atomic_json(self.provider.active_path, {
            "collection": "active", "manifest": self.provider.manifest.__dict__,
        })
        self.principal = Principal("agent.alpha", "agent", "tok", frozenset({"memory.read"}))

    @staticmethod
    def _note():
        return {
            "id": "mem_00000000000000000000000000000001", "author": "agent.alpha",
            "visibility": "private", "recordedAt": "2026-07-19T00:00:00Z",
            "title": "x", "body": "y",
        }

    def _point(self, note, *, content_hash=None):
        digest = content_hash or hashlib.sha256(
            json.dumps(note, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        return SimpleNamespace(score=0.2, payload={
            "noteId": note["id"], "recordedEpoch": 1.0, "contentHash": digest,
        })

    def test_authorization_filter_is_pushed_into_both_prefetches_and_outer_query(self):
        note = self._note()
        client = _Client([self._point(note)])
        self.provider._client = lambda: client
        results = self.provider.search(
            "query", self.principal, load_note=lambda _: note,
            can_read=lambda principal, value: True,
        )
        self.assertEqual([note], results)
        self.assertIsNotNone(client.call["query_filter"])
        self.assertEqual(2, len(client.call["prefetch"]))
        self.assertTrue(all(prefetch.filter is not None for prefetch in client.call["prefetch"]))

    def test_stale_payload_is_final_authorized_fail_closed(self):
        note = self._note()
        client = _Client([self._point(note, content_hash="stale")])
        self.provider._client = lambda: client
        results = self.provider.search(
            "query", self.principal, load_note=lambda _: note,
            can_read=lambda principal, value: True,
        )
        self.assertEqual([], results)

    def test_current_policy_denial_is_final_authorized_fail_closed(self):
        note = self._note()
        client = _Client([self._point(note)])
        self.provider._client = lambda: client
        results = self.provider.search(
            "query", self.principal, load_note=lambda _: note,
            can_read=lambda principal, value: False,
        )
        self.assertEqual([], results)

    def test_provider_failure_falls_back_to_keyword_without_hiding_reason(self):
        class BrokenProvider:
            last_fallback_reason = None
            def search(self, *args, **kwargs):
                raise ConnectionError("derived provider unavailable")
            def status(self):
                return {"provider": "qdrant", "fallbackReason": self.last_fallback_reason}

        provider = BrokenProvider()
        service = MemoryService(
            self.temp.name,
            new_note_id=lambda: "mem_11111111111111111111111111111111",
            search_provider=provider,
        )
        owner = Principal("agent.alpha", "agent", "tok", frozenset({"memory.read", "memory.write"}))
        service.create_note(
            {"type": "observation", "title": "fallback marker", "visibility": "private", "body": "keyword survives"},
            owner, idempotency_key="fallback",
        )
        self.assertEqual(1, len(service.search("keyword survives", owner, mode="auto")))
        self.assertEqual("ConnectionError", provider.last_fallback_reason)

    def test_model_cache_revision_is_enforced_not_merely_recorded(self):
        source = "vendor/model"
        expected = "a" * 40
        repository = self.provider.cache_dir / "models--vendor--model"
        (repository / "refs").mkdir(parents=True)
        (repository / "snapshots" / expected).mkdir(parents=True)
        (repository / "refs" / "main").write_text(expected, encoding="utf-8")
        self.provider._verify_cache_revision(source, expected)
        (repository / "refs" / "main").write_text("b" * 40, encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "revision mismatch"):
            self.provider._verify_cache_revision(source, expected)

    def test_product_manifest_pins_license_dimension_and_tokenizer_artifacts(self):
        manifest = self.provider.manifest
        self.assertEqual("apache-2.0", manifest.dense_license)
        self.assertEqual("apache-2.0", manifest.sparse_license)
        self.assertEqual(384, manifest.dense_dimension)
        self.assertEqual("63f894a4ce2e6b4a610a1b9b33667a27ebce95eef77bb02ad0e32a5147933d4c", manifest.tokenizer_config_hash)
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is supplied by the contracts extra")
        schema = json.loads((Path(__file__).parents[2] / "schemas" / "retrieval-v1.json").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(asdict(manifest))


if __name__ == "__main__":
    unittest.main()
