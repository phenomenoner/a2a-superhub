from __future__ import annotations

import sys
import threading
from pathlib import Path

from a2a_superhub.auth import Principal
from a2a_superhub.memory import MemoryService
from a2a_superhub.store import HubStore


def block_at(target: str):
    def callback(stage: str) -> None:
        if stage == target:
            print(f"READY:{stage}", flush=True)
            threading.Event().wait()

    return callback


mode = sys.argv[1]
state = Path(sys.argv[2])

if mode.startswith("memory:"):
    stage = mode.split(":", 1)[1]
    service = MemoryService(
        state,
        now=lambda: "2026-07-19T12:00:00Z",
        new_note_id=lambda: "mem_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    service.create_note(
        {"type": "note", "title": "kill scenario", "visibility": "private", "body": "durable after restart"},
        Principal("agent.alpha", "agent", "tok_worker", frozenset({"memory.read", "memory.write", "memory.share"})),
        idempotency_key="kill-idem",
        failpoint=block_at(stage),
    )
elif mode.startswith("index-rebuild:"):
    stage = mode.split(":", 1)[1]
    MemoryService(state).rebuild_index(failpoint=block_at(stage))
elif mode.startswith("index-upsert:"):
    stage = mode.split(":", 1)[1]
    MemoryService(state).process_jobs(failpoint=block_at(stage))
elif mode.startswith("event:"):
    stage = mode.split(":", 1)[1]
    store = HubStore(state)
    store.append_event(
        "task_kill",
        "task.completed",
        {"result": "ok"},
        state="completed",
        failpoint=block_at(stage),
    )
elif mode.startswith("delivery:"):
    stage = mode.split(":", 1)[1]
    MemoryService(state, enable_delivery=True).create_note(
        {"type": "handoff", "title": "delivery kill", "visibility": "direct:agent.beta", "body": "deliver after restart"},
        Principal("agent.alpha", "agent", "tok_worker", frozenset({"memory.read", "memory.write", "memory.share"})),
        idempotency_key="delivery-kill",
        failpoint=block_at(stage),
    )
elif mode.startswith("fetch:"):
    stage = mode.split(":", 1)[1]
    MemoryService(state, enable_delivery=True).fetch_inbox(
        Principal("agent.beta", "agent", "tok_beta", frozenset({"memory.read"})),
        "desktop.a",
        failpoint=block_at(stage),
    )
elif mode.startswith("ack:"):
    stage = mode.split(":", 1)[1]
    MemoryService(state, enable_delivery=True).acknowledge_inbox(
        Principal("agent.beta", "agent", "tok_beta", frozenset({"memory.read"})),
        "desktop.a",
        sys.argv[3],
        failpoint=block_at(stage),
    )
elif mode.startswith("tasklog:"):
    stage = mode.split(":", 1)[1]
    MemoryService(state, enable_task_log=True, task_log_intents={"memory.sediment"}).replay_terminal_outbox(
        HubStore(state), failpoint=block_at(stage)
    )
elif mode.startswith("missing-id:"):
    stage = mode.split(":", 1)[1]
    MemoryService(state, enable_watcher_side_effects=True).sync_filesystem(failpoint=block_at(stage))
else:  # pragma: no cover
    raise SystemExit(f"unknown mode: {mode}")
