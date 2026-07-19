#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import tempfile
import time
import uuid
from pathlib import Path

from a2a_superhub.auth import Principal
from a2a_superhub.memory import MemoryService, NOTE_SCHEMA, atomic_write, note_path, serialize_note


ADMIN = Principal(
    "local.operator", "operator", "tok_benchmark_admin",
    frozenset({"memory.read", "memory.write", "memory.share", "memory.admin"}),
)
RECEIVER = Principal("agent.beta", "agent", "tok_benchmark_beta", frozenset({"memory.read"}))


def timed(callable_):
    started = time.perf_counter()
    result = callable_()
    return result, time.perf_counter() - started


def fixture_note(index: int) -> dict:
    note_id = f"mem_{index + 1:032x}"
    return {
        "schema": NOTE_SCHEMA,
        "id": note_id,
        "type": "observation",
        "title": f"Baseline observation {index + 1}",
        "author": "agent.alpha",
        "visibility": "shared",
        "recordedAt": "2026-01-01T00:00:00Z",
        "source": {"kind": "filesystem", "relativePath": f"benchmark/{index + 1}"},
        "project": "memory-foundation-baseline",
        "participants": ["agent.alpha", "agent.beta"],
        "about": ["agent.beta"],
        "tags": ["benchmark", "baseline"],
        "body": f"Real SQLite baseline corpus item {index + 1}; searchable needle-{index % 97}.",
    }


def run_size(size: int, state: Path) -> dict:
    service = MemoryService(state)
    service.init()

    def write_all():
        for index in range(size):
            note = fixture_note(index)
            atomic_write(note_path(service.root, note["id"]), serialize_note(note))

    _, write_seconds = timed(write_all)
    indexed_count, rebuild_seconds = timed(service.rebuild_index)
    query = "searchable baseline"
    hits, fts_seconds = timed(lambda: service.search(query, ADMIN, limit=50))

    delivery = MemoryService(state, enable_delivery=True)
    _, startup_seconds = timed(delivery.init)
    wakeup, wakeup_seconds = timed(lambda: delivery.wakeup(RECEIVER, "benchmark-runtime", budget_bytes=65_536))
    stats = delivery.stats(ADMIN)
    return {
        "notes": size,
        "write": {"seconds": write_seconds, "notesPerSecond": size / write_seconds if write_seconds else None},
        "rebuild": {"seconds": rebuild_seconds, "indexed": indexed_count, "notesPerSecond": size / rebuild_seconds if rebuild_seconds else None},
        "fts": {"seconds": fts_seconds, "hits": len(hits), "query": query},
        "deliveryStartup": {"seconds": startup_seconds, "deliveries": stats["deliveryBacklog"]},
        "wakeup": {
            "seconds": wakeup_seconds,
            "role": wakeup["role"],
            "items": sum(len(section["items"]) for section in wakeup["sections"]),
            "bytes": len(json.dumps(wakeup, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
            "truncated": wakeup["truncated"],
        },
    }


def write_checkpoint(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Durable-memory filesystem and SQLite baseline")
    parser.add_argument("--sizes", nargs="+", type=int, default=[1, 1000, 10000])
    parser.add_argument("--output", help="Optional JSON output path")
    args = parser.parse_args(argv)
    if any(size < 1 or size > 10000 for size in args.sizes):
        parser.error("sizes must be between 1 and 10000")
    result = {
        "schema": "a2a-superhub.memory-foundation-baseline.v1",
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "logicalCpuCount": os.cpu_count(),
            "processor": platform.processor() or "not-reported",
        },
        "configuration": {
            "storage": "real temporary filesystem plus SQLite/FTS5",
            "journal": "ops WAL; derived index DELETE",
            "delivery": "one about delivery per note",
            "wakeupBudgetBytes": 65536,
            "warmup": "none; one measured run per size",
        },
        "results": [],
    }
    for size in args.sizes:
        with tempfile.TemporaryDirectory(prefix=f"a2a-memory-foundation-baseline-{size}-") as tmp:
            result["results"].append(run_size(size, Path(tmp)))
        if args.output:
            write_checkpoint(Path(args.output), result)
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
