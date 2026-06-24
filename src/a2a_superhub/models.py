from __future__ import annotations

import json
import time
import uuid
from typing import Any

TASK_STATES = {
    "submitted",
    "accepted",
    "working",
    "input-required",
    "completed",
    "failed",
    "canceled",
    "rejected",
    "dead-lettered",
}

TERMINAL_STATES = {"completed", "failed", "canceled", "rejected", "dead-lettered"}


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def ensure_object(value: Any, *, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def ensure_state(state: str) -> str:
    if state not in TASK_STATES:
        allowed = ", ".join(sorted(TASK_STATES))
        raise ValueError(f"unsupported task state {state!r}; expected one of: {allowed}")
    return state
