from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError:
    Draft202012Validator = None
    FormatChecker = None

from a2a_superhub.server import make_server


ROOT = Path(__file__).resolve().parents[2]


def request_json(url: str, *, token: str, payload: dict | None = None, headers: dict | None = None) -> dict:
    raw = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", **(headers or {})}
    request = urllib.request.Request(url, data=raw, headers=request_headers, method="POST" if raw is not None else "GET")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


@unittest.skipIf(Draft202012Validator is None, "install the contracts extra for live schema validation")
class LiveMemoryHttpContractTests(unittest.TestCase):
    def test_create_search_inbox_ack_wakeup_and_error_match_closed_schema(self) -> None:
        schema = json.loads((ROOT / "schemas" / "memory-api-v1.schema.json").read_text(encoding="utf-8"))

        def validate(definition: str, instance: dict) -> None:
            selected = {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": f"#/$defs/{definition}"}
            errors = list(Draft202012Validator(selected, format_checker=FormatChecker()).iter_errors(instance))
            self.assertEqual([], errors, (definition, instance, errors))

        with tempfile.TemporaryDirectory() as tmp:
            principals = {
                "owner": {"subject": "agent.alpha", "kind": "agent", "tokenId": "tok_owner", "scopes": ["memory.read", "memory.write", "memory.share"]},
                "beta": {"subject": "agent.beta", "kind": "agent", "tokenId": "tok_beta", "scopes": ["memory.read"]},
            }
            server = make_server(tmp, port=0, enable_memory=True, enable_delivery=True, principals=principals)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                created = request_json(
                    base + "/v1/memory/notes", token="owner",
                    payload={"type": "observation", "title": "contract", "visibility": "shared", "about": ["agent.beta"], "body": "contract marker"},
                    headers={"Idempotency-Key": "live-contract"},
                )
                validate("createNoteResponse", created)
                search = request_json(base + "/v1/memory/search?q=contract", token="owner")
                self.assertEqual(1, len(search["items"]))
                validate("notePage", search)
                inbox = request_json(base + "/v1/memory/inbox?consumerId=desktop.a", token="beta")
                self.assertEqual(1, len(inbox["items"]))
                validate("inboxPage", inbox)
                validate("wakeupResponse", request_json(base + "/v1/memory/wakeup?consumerId=desktop.a", token="beta"))
                validate("ackResponse", request_json(
                    base + "/v1/memory/inbox/ack", token="beta",
                    payload={"consumerId": "desktop.a", "cursor": inbox["cursor"]},
                ))
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    request_json(
                        base + "/v1/memory/inbox/ack", token="beta",
                        payload={"consumerId": "desktop.a", "cursor": inbox["cursor"] + "x"},
                    )
                validate("errorEnvelope", json.loads(raised.exception.read().decode("utf-8")))
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
