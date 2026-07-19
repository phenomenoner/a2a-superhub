from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path


FIXTURES = Path(__file__).with_name("fixtures")


class ContractScenarioRunner:
    def __init__(self, fixture: dict):
        self.state = copy.deepcopy(fixture["initial"])
        self.idempotency: dict[tuple[str, str], int] = {}

    def run(self, operations: list[dict]) -> dict:
        for operation in operations:
            getattr(self, f"op_{operation['op'].replace('-', '_')}")(operation)
        return self.state

    def op_persist_note(self, operation: dict) -> None:
        self.state["noteDurable"] = True

    def op_failpoint(self, operation: dict) -> None:
        self.state["lastFailpoint"] = operation["name"]

    def op_recover(self, operation: dict) -> None:
        if self.state.get("noteDurable"):
            self.state["jobDurable"] = True

    def op_retry(self, operation: dict) -> None:
        key = operation["key"]
        request_hash = operation["requestHash"]
        prior_hash = next((stored_hash for (stored_key, stored_hash) in self.idempotency if stored_key == key), None)
        if prior_hash is None:
            self.idempotency[(key, request_hash)] = 201
            self.state["logicalResults"] = self.state.get("logicalResults", 0) + 1
            self.state["response"] = 201
        elif prior_hash == request_hash:
            self.state["response"] = self.idempotency[(key, request_hash)]
        else:
            self.state["response"] = 409

    def op_ack(self, operation: dict) -> None:
        binding_matches = operation["principal"] == self.state["principal"] and operation["consumerId"] == self.state["consumerId"]
        was_issued = operation["sequence"] in self.state["issued"]
        if not binding_matches or not was_issued:
            self.state["response"] = "CURSOR_INVALID"
        elif operation["sequence"] < self.state["acked"]:
            self.state["response"] = "ACK_STALE"
        else:
            self.state["acked"] = operation["sequence"]
            self.state["response"] = "ACKED"

    def op_final_authorize(self, operation: dict) -> None:
        candidate = self.state["candidate"]
        current = self.state["authoritative"]
        if candidate["revision"] != current["revision"]:
            self.state["degraded"].append("STALE_VISIBILITY_REVISION")
        self.state["emitted"] = current["visibility"] == "shared" or self.state["requester"] == current["author"]

    def op_wrap_memory(self, operation: dict) -> None:
        self.state["role"] = "data"
        self.state["trust"] = "untrusted-memory"
        self.state["text"] = operation["text"]


class ContractScenarioTests(unittest.TestCase):
    def test_all_skeletons_are_deterministic_and_replayable(self) -> None:
        paths = sorted(FIXTURES.glob("*.json"))
        self.assertGreaterEqual(len(paths), 5)
        ids = set()
        for path in paths:
            fixture = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn(fixture["id"], ids)
            ids.add(fixture["id"])
            first = ContractScenarioRunner(fixture).run(fixture["operations"])
            second = ContractScenarioRunner(fixture).run(fixture["operations"])
            self.assertEqual(first, second, path.name)
            for key, expected in fixture["expected"].items():
                self.assertEqual(expected, first.get(key), (path.name, key))


if __name__ == "__main__":
    unittest.main()
