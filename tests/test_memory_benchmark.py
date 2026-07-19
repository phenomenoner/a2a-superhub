import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.memory_foundation_baseline import main, run_size


class MemoryBaselineHarnessTests(unittest.TestCase):
    def test_real_small_corpus_runs_all_measured_surfaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_size(10, Path(tmp))
        self.assertEqual(result["notes"], 10)
        self.assertEqual(result["rebuild"]["indexed"], 10)
        self.assertGreater(result["fts"]["hits"], 0)
        self.assertEqual(result["deliveryStartup"]["deliveries"], 10)
        self.assertEqual(result["wakeup"]["role"], "data")

    def test_each_completed_size_is_atomically_checkpointed_before_next_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "checkpoint.json"
            with patch(
            "benchmarks.memory_foundation_baseline.run_size",
                side_effect=[{"notes": 1, "complete": True}, RuntimeError("next size interrupted")],
            ):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    main(["--sizes", "1", "2", "--output", str(output)])
            import json
            checkpoint = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual([{"notes": 1, "complete": True}], checkpoint["results"])
            self.assertEqual([], list(output.parent.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
