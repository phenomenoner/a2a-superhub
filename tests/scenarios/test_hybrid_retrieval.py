from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from a2a_superhub.auth import Principal
from a2a_superhub.memory import MemoryService
from a2a_superhub.retrieval import QdrantRetrievalProvider, enforce_thresholds, evaluate_rankings


FIXTURES = Path(__file__).parents[1] / "retrieval" / "fixtures"


@unittest.skipUnless(
    os.environ.get("A2A_SUPERHUB_RUN_SEARCH_INTEGRATION") == "1",
    "set A2A_SUPERHUB_RUN_SEARCH_INTEGRATION=1 with the search extra installed",
)
class HybridRetrievalScenarioTests(unittest.TestCase):
    def test_resume_swap_acl_quality_and_ops_isolation(self):
        corpus = json.loads((FIXTURES / "retrieval-eval-corpus.json").read_text(encoding="utf-8"))
        thresholds = json.loads((FIXTURES / "retrieval-eval-thresholds.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            service = MemoryService(temporary)
            service.init()
            ops_before = hashlib.sha256(service.ops_path.read_bytes()).hexdigest()
            mode = os.environ.get("A2A_SUPERHUB_SEARCH_TEST_MODE", "local")
            provider = QdrantRetrievalProvider(
                temporary, mode=mode,
                url=os.environ.get("A2A_SUPERHUB_SEARCH_TEST_URL"),
                cache_dir=os.environ.get("A2A_SUPERHUB_SEARCH_TEST_CACHE"),
                chunk_chars=240, chunk_overlap=32,
            )
            with self.assertRaisesRegex(RuntimeError, "interruption"):
                provider.rebuild(corpus["notes"], fail_after_chunks=3)
            self.assertFalse(provider.active_path.exists())
            completed = provider.rebuild(corpus["notes"])
            self.assertTrue(completed["resumed"])
            self.assertEqual(ops_before, hashlib.sha256(service.ops_path.read_bytes()).hexdigest())

            notes = {note["id"]: note for note in corpus["notes"]}
            rankings = {}
            unauthorized = 0
            for query in corpus["queries"]:
                principal = Principal(query["principal"], "agent", "test", frozenset({"memory.read"}))
                results = provider.search(
                    query["text"], principal, load_note=notes.__getitem__,
                    can_read=MemoryService._can_read, limit=10,
                )
                rankings[query["id"]] = [note["id"] for note in results]
                unauthorized += sum(not MemoryService._can_read(principal, note) for note in results)
                self.assertFalse(set(query.get("expectedHidden", [])) & set(rankings[query["id"]]))
            metrics = evaluate_rankings(rankings, {q["id"]: q["relevance"] for q in corpus["queries"]})
            enforce_thresholds({
                "hybridRecallAt5": metrics["recallAt5"],
                "hybridNdcgAt10": metrics["ndcgAt10"],
                "unauthorizedResults": unauthorized,
            }, thresholds)

            changed = json.loads(json.dumps(corpus["notes"]))
            changed[0]["body"] += " authoritative revision changed"
            stale = provider.search(
                "gateway restart", Principal("agent.alpha", "agent", "test", frozenset({"memory.read"})),
                load_note={note["id"]: note for note in changed}.__getitem__,
                can_read=MemoryService._can_read, limit=20,
            )
            self.assertNotIn(changed[0]["id"], {note["id"] for note in stale})
            swapped = provider.rebuild(changed)
            self.assertEqual(completed["collection"], swapped["previousCollection"])
            self.assertNotEqual(completed["collection"], swapped["collection"])
            self.assertEqual(ops_before, hashlib.sha256(service.ops_path.read_bytes()).hexdigest())

            with tempfile.TemporaryDirectory() as other_state:
                other = QdrantRetrievalProvider(
                    other_state, mode=mode,
                    url=os.environ.get("A2A_SUPERHUB_SEARCH_TEST_URL"),
                    cache_dir=os.environ.get("A2A_SUPERHUB_SEARCH_TEST_CACHE"),
                    chunk_chars=240, chunk_overlap=32,
                )
                isolated = other.rebuild(corpus["notes"])
                self.assertNotEqual(completed["collection"], isolated["collection"])


if __name__ == "__main__":
    unittest.main()
