from __future__ import annotations

import json
import unittest
from pathlib import Path

from a2a_superhub.memory import serialize_note


FIXTURES = Path(__file__).with_name("fixtures")


class RetrievalEvaluationContractTests(unittest.TestCase):
    def test_retrieval_schema_uses_the_public_canonical_id(self) -> None:
        schema = json.loads(
            (Path(__file__).resolve().parents[2] / "schemas" / "retrieval-v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            "https://phenomenoner.github.io/a2a-superhub/schemas/retrieval-v1.json",
            schema["$id"],
        )

    def test_sanitized_corpus_and_pre_result_thresholds_are_frozen(self) -> None:
        corpus = json.loads((FIXTURES / "retrieval-eval-corpus.json").read_text(encoding="utf-8"))
        thresholds = json.loads((FIXTURES / "retrieval-eval-thresholds.json").read_text(encoding="utf-8"))
        self.assertEqual("a2a-superhub.retrieval-evaluation-corpus.v1", corpus["schema"])
        self.assertEqual("a2a-superhub.retrieval-evaluation-thresholds.v1", thresholds["schema"])
        self.assertTrue(thresholds["frozenBeforeResults"])
        self.assertFalse(corpus["sanitization"]["containsProductionData"])
        self.assertFalse(corpus["sanitization"]["containsSecrets"])
        self.assertTrue(corpus["sanitization"]["textIsUntrustedData"])
        self.assertGreaterEqual(len(corpus["notes"]), 16)
        self.assertGreaterEqual(len(corpus["queries"]), 15)
        note_ids = {note["id"] for note in corpus["notes"]}
        self.assertEqual(len(corpus["notes"]), len(note_ids))
        for note in corpus["notes"]:
            self.assertTrue(serialize_note(note).startswith(b"---\n"))
        categories = {query["category"] for query in corpus["queries"]}
        self.assertTrue({
            "exact-id", "exact-filename", "agent-id", "chinese", "english",
            "code-switch", "paraphrase", "recency", "superseded", "disputed",
            "mixed-visibility", "short-note", "long-note", "prompt-like",
        }.issubset(categories))
        for query in corpus["queries"]:
            self.assertTrue(query["relevance"])
            self.assertTrue(set(query["relevance"]).issubset(note_ids))
            self.assertTrue(set(query.get("expectedHidden", [])).issubset(note_ids))
        self.assertEqual(0, thresholds["quality"]["unauthorizedResultsMax"])
        self.assertEqual(0, thresholds["quality"]["unauthorizedScoresOrSnippetsMax"])
        self.assertTrue(thresholds["fallback"]["keywordFallbackRequired"])
        self.assertTrue(thresholds["switchGate"]["fixedPointCountTriggerForbidden"])

    def test_metrics_and_threshold_enforcement_are_machine_executable(self) -> None:
        from a2a_superhub.retrieval import evaluate_rankings, enforce_thresholds

        judgments = {"q": {"mem_a": 3, "mem_b": 1}}
        metrics = evaluate_rankings({"q": ["mem_a", "mem_x", "mem_b"]}, judgments)
        self.assertEqual(1.0, metrics["recallAt5"])
        self.assertGreater(metrics["ndcgAt10"], 0.9)
        with self.assertRaises(ValueError):
            enforce_thresholds(
                {"hybridRecallAt5": 0.5, "hybridNdcgAt10": 0.5, "unauthorizedResults": 1},
                {"quality": {"hybridRecallAt5Min": 0.9, "hybridNdcgAt10Min": 0.85,
                             "unauthorizedResultsMax": 0}},
            )

    def test_chunk_ids_and_manifest_compatibility_are_deterministic(self) -> None:
        from a2a_superhub.retrieval import EmbeddingManifest, deterministic_chunks

        note = json.loads((FIXTURES / "retrieval-eval-corpus.json").read_text(encoding="utf-8"))["notes"][12]
        first = deterministic_chunks(note, max_chars=160, overlap_chars=24)
        second = deterministic_chunks(note, max_chars=160, overlap_chars=24)
        self.assertEqual(first, second)
        self.assertGreater(len(first), 1)
        self.assertEqual(len(first), len({chunk["chunkId"] for chunk in first}))
        baseline = EmbeddingManifest.create(
            dense_model="candidate/dense", dense_revision="rev-a", dense_dimension=384,
            sparse_model="candidate/sparse", sparse_revision="rev-s", chunk_chars=160,
            chunk_overlap=24,
        )
        self.assertTrue(baseline.compatible_with(baseline))
        changed = EmbeddingManifest.create(
            dense_model="candidate/dense", dense_revision="rev-b", dense_dimension=384,
            sparse_model="candidate/sparse", sparse_revision="rev-s", chunk_chars=160,
            chunk_overlap=24,
        )
        self.assertFalse(baseline.compatible_with(changed))


if __name__ == "__main__":
    unittest.main()
