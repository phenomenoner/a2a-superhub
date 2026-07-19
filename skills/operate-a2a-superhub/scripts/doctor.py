#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys

from a2a_superhub.client import HubClient, HubClientError


def inspect_hub(url: str, token: str | None) -> dict:
    client = HubClient(url, token=token)
    result = {
        "schema": "a2a-superhub.skill-doctor.v1",
        "target": url,
        "readOnly": True,
        "health": client.health(),
        "ready": client.ready(),
    }
    capabilities = client.negotiate()
    result["capabilities"] = capabilities
    result["compatibility"] = capabilities.get("compatibility")
    principal = capabilities.get("principal") if isinstance(capabilities, dict) else None
    scopes = principal.get("scopes", []) if isinstance(principal, dict) else []
    if "memory.admin" in scopes or "hub.admin" in scopes:
        result["memoryStats"] = client.request("GET", "/v1/memory/stats")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only A2A Superhub capability and health doctor")
    parser.add_argument("--url", default=os.environ.get("A2A_SUPERHUB_URL", "http://127.0.0.1:8787"))
    parser.add_argument("--token-env", default="A2A_SUPERHUB_TOKEN")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args(argv)
    token = os.environ.get(args.token_env)
    try:
        result = inspect_hub(args.url, token)
    except HubClientError as exc:
        result = {
            "schema": "a2a-superhub.skill-doctor.v1",
            "ok": False,
            "readOnly": True,
            "error": {"kind": exc.kind, "status": exc.status, "code": exc.code, "message": str(exc)},
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 3 if exc.kind == "auth" else 4 if exc.kind == "connection" else 2
    result["ok"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
