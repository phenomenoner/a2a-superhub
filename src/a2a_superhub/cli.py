from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore
from .server import run_server
from .store import HubStore


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _load_json(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def cmd_init(args: argparse.Namespace) -> int:
    HubStore(args.state).init()
    ArtifactStore(args.state).init()
    _print({"status": "initialized", "stateDir": str(Path(args.state))})
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    token = args.token or os.environ.get("A2A_SUPERHUB_TOKEN")
    run_server(args.state, host=args.host, port=args.port, token=token)
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    _print(HubStore(args.state).health())
    return 0


def cmd_agent_register(args: argparse.Namespace) -> int:
    card = _load_json(args.file)
    _print(HubStore(args.state).register_agent(card))
    return 0


def cmd_agent_list(args: argparse.Namespace) -> int:
    _print({"agents": HubStore(args.state).list_agents()})
    return 0


def cmd_task_create(args: argparse.Namespace) -> int:
    task = _load_json(args.file)
    if not task:
        task = {
            "fromAgent": args.from_agent,
            "toAgent": args.to_agent,
            "intent": args.intent,
            "idempotencyKey": args.idempotency_key,
            "payload": {"summary": args.summary or ""},
            "permissions": {"sideEffects": "default-deny", "scopes": args.scope or []},
        }
    record, inserted = HubStore(args.state).create_task(task)
    _print({"inserted": inserted, "task": record})
    return 0


def cmd_task_get(args: argparse.Namespace) -> int:
    task = HubStore(args.state).get_task(args.task_id)
    if not task:
        _print({"error": "task not found", "taskId": args.task_id})
        return 2
    _print(task)
    return 0


def cmd_task_list(args: argparse.Namespace) -> int:
    _print({"tasks": HubStore(args.state).list_tasks(limit=args.limit)})
    return 0


def cmd_task_event(args: argparse.Namespace) -> int:
    payload = _load_json(args.payload_file) if args.payload_file else {"message": args.message or ""}
    event = HubStore(args.state).append_event(args.task_id, args.kind, payload, state=args.state_value)
    _print({"event": event, "task": HubStore(args.state).get_task(args.task_id)})
    return 0


def cmd_task_events(args: argparse.Namespace) -> int:
    _print({"events": HubStore(args.state).list_events(args.task_id)})
    return 0


def cmd_task_cancel(args: argparse.Namespace) -> int:
    event = HubStore(args.state).append_event(args.task_id, "task.canceled", {"reason": args.reason}, state="canceled")
    _print({"event": event, "task": HubStore(args.state).get_task(args.task_id)})
    return 0


def cmd_artifact_put(args: argparse.Namespace) -> int:
    data = Path(args.file).read_bytes()
    manifest = ArtifactStore(args.state).put_bytes(
        data,
        filename=args.filename or Path(args.file).name,
        media_type=args.media_type,
        created_by=args.created_by,
    )
    _print(manifest)
    return 0


def cmd_artifact_get(args: argparse.Namespace) -> int:
    store = ArtifactStore(args.state)
    manifest = store.get_manifest(args.artifact_id)
    if not manifest:
        _print({"error": "artifact not found", "artifactId": args.artifact_id})
        return 2
    if args.content:
        data = store.get_bytes(args.artifact_id)
        if data is None:
            _print({"error": "artifact blob missing", "artifactId": args.artifact_id})
            return 2
        if args.output:
            Path(args.output).write_bytes(data)
            _print({"artifactId": args.artifact_id, "output": args.output, "bytes": len(data)})
        else:
            _print({"artifactId": args.artifact_id, "contentBase64": base64.b64encode(data).decode("ascii")})
    else:
        _print(manifest)
    return 0


def cmd_artifact_list(args: argparse.Namespace) -> int:
    _print({"artifacts": ArtifactStore(args.state).list_manifests()})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="a2a-superhub", description="Standalone A2A task and artifact hub")
    parser.add_argument("--state", default="state", help="Hub state directory")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="Initialize hub state")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("serve", help="Run the HTTP/A2A hub")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--token", help="Optional bearer token; otherwise A2A_SUPERHUB_TOKEN")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("health", help="Print hub health from local state")
    p.set_defaults(func=cmd_health)

    agent = sub.add_parser("agent", help="Agent registry commands").add_subparsers(dest="agent_command", required=True)
    p = agent.add_parser("register", help="Register an Agent Card JSON file")
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_agent_register)
    p = agent.add_parser("list", help="List registered agents")
    p.set_defaults(func=cmd_agent_list)

    task = sub.add_parser("task", help="Task lifecycle commands").add_subparsers(dest="task_command", required=True)
    p = task.add_parser("create", help="Create a task")
    p.add_argument("--file", help="Task JSON file")
    p.add_argument("--from-agent")
    p.add_argument("--to-agent")
    p.add_argument("--intent", default="agent.query")
    p.add_argument("--summary")
    p.add_argument("--idempotency-key")
    p.add_argument("--scope", action="append")
    p.set_defaults(func=cmd_task_create)
    p = task.add_parser("get", help="Get a task")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_task_get)
    p = task.add_parser("list", help="List tasks")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_task_list)
    p = task.add_parser("event", help="Append a task event")
    p.add_argument("task_id")
    p.add_argument("--kind", default="task.progress")
    p.add_argument("--state-value", choices=["submitted", "accepted", "working", "input-required", "completed", "failed", "canceled", "rejected", "dead-lettered"])
    p.add_argument("--message")
    p.add_argument("--payload-file")
    p.set_defaults(func=cmd_task_event)
    p = task.add_parser("events", help="List task events")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_task_events)
    p = task.add_parser("cancel", help="Cancel a task")
    p.add_argument("task_id")
    p.add_argument("--reason", default="cancel requested")
    p.set_defaults(func=cmd_task_cancel)

    artifact = sub.add_parser("artifact", help="Artifact commands").add_subparsers(dest="artifact_command", required=True)
    p = artifact.add_parser("put", help="Store an artifact")
    p.add_argument("--file", required=True)
    p.add_argument("--filename")
    p.add_argument("--media-type", default="application/octet-stream")
    p.add_argument("--created-by", default="unknown")
    p.set_defaults(func=cmd_artifact_put)
    p = artifact.add_parser("get", help="Get artifact manifest or content")
    p.add_argument("artifact_id")
    p.add_argument("--content", action="store_true")
    p.add_argument("--output")
    p.set_defaults(func=cmd_artifact_get)
    p = artifact.add_parser("list", help="List artifacts")
    p.set_defaults(func=cmd_artifact_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
