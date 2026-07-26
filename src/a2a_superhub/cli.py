from __future__ import annotations

import argparse
import base64
import json
import os
import uuid
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore
from .server import run_server
from .store import HubStore
from .auth import Principal
from .derivation import DerivationService
from .memory import MemoryError as HubMemoryError, MemoryService
from .operations import (
    BackupManager,
    OperationsDiagnostics,
    OperationsError,
    RetentionManager,
    SearchMigrationManager,
    load_search_provider_config,
)
from .skill_package import SkillInstallError, install_skill, skill_source_path, uninstall_skill, validate_skill


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
    principals = _load_json(args.principals) if args.principals else None
    search_mode, search_url = _resolved_search_provider(args)
    run_server(
        args.state, host=args.host, port=args.port, token=token, enable_memory=args.enable_memory,
        enable_delivery=args.enable_delivery, enable_task_log=args.enable_task_log,
        enable_watcher_side_effects=args.enable_watcher_side_effects,
        task_log_intents=set(args.task_log_intent or []), principals=principals,
        search_mode=search_mode, search_url=search_url,
        search_cache_dir=args.search_cache_dir,
        enable_derivers=args.enable_derivers, max_artifact_bytes=args.max_artifact_bytes,
    )
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
        visibility=args.visibility,
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


def cmd_artifact_derive(args: argparse.Namespace) -> int:
    artifacts = ArtifactStore(args.state)
    memory = MemoryService(args.state, artifact_store=artifacts)
    memory.init()
    service = DerivationService(args.state, artifacts, memory)
    service.init()
    _print(service.derive(args.artifact_id, _cli_principal(args), retry=args.retry))
    return 0


def cmd_artifact_derivation_status(args: argparse.Namespace) -> int:
    artifacts = ArtifactStore(args.state)
    memory = MemoryService(args.state, artifact_store=artifacts)
    memory.init()
    service = DerivationService(args.state, artifacts, memory)
    service.init()
    _print(service.status(args.job_id))
    return 0


def cmd_artifact_derivation_purge(args: argparse.Namespace) -> int:
    artifacts = ArtifactStore(args.state)
    memory = MemoryService(args.state, artifact_store=artifacts)
    memory.init()
    service = DerivationService(args.state, artifacts, memory)
    service.init()
    _print(service.purge(args.job_id, _cli_principal(args)))
    return 0


def _cli_principal(args: argparse.Namespace) -> Principal:
    return Principal("local.operator", "operator", "tok_cli", frozenset({"hub.admin"}))


def cmd_memory_note_create(args: argparse.Namespace) -> int:
    request = _load_json(args.file)
    result = MemoryService(args.state).create_note(
        request,
        _cli_principal(args),
        idempotency_key=args.idempotency_key,
        source_kind="cli",
        contract_version=2,
    )
    _print({"inserted": result.inserted, "revision": result.revision, "traceId": result.trace_id, "note": result.note})
    return 0


def cmd_memory_note_read(args: argparse.Namespace) -> int:
    try:
        service = MemoryService(args.state)
        note = service.read_note(args.note_id, _cli_principal(args))
    except KeyError:
        _print({"error": "note not found", "noteId": args.note_id})
        return 2
    if args.include_lifecycle:
        _print(
            {
                "note": note,
                "lifecycle": service.note_lifecycle(
                    args.note_id, _cli_principal(args)
                ),
            }
        )
    else:
        _print(note)
    return 0


def cmd_memory_reindex(args: argparse.Namespace) -> int:
    count = MemoryService(args.state).rebuild_index()
    _print({"status": "rebuilt", "notes": count})
    return 0


def _search_service(args: argparse.Namespace) -> MemoryService:
    search_mode, search_url = _resolved_search_provider(args)
    provider = None
    if search_mode in {"local", "server"}:
        from .retrieval import QdrantRetrievalProvider
        provider = QdrantRetrievalProvider(
            args.state, mode=search_mode, url=search_url,
            cache_dir=args.search_cache_dir,
        )
    return MemoryService(args.state, search_provider=provider)


def _resolved_search_provider(args: argparse.Namespace) -> tuple[str, str | None]:
    if args.search_mode != "configured":
        return args.search_mode, args.search_url
    configured = load_search_provider_config(args.state)
    return str(configured["mode"]), configured.get("url")


def cmd_memory_search(args: argparse.Namespace) -> int:
    service = _search_service(args)
    items = service.search(args.query, _cli_principal(args), limit=args.limit, mode=args.mode)
    _print({"items": items, "search": service.search_status()})
    return 0


def cmd_memory_search_reindex(args: argparse.Namespace) -> int:
    result = _search_service(args).rebuild_search_index()
    _print(result)
    return 0


def cmd_memory_inbox_fetch(args: argparse.Namespace) -> int:
    service = MemoryService(args.state, enable_delivery=True)
    _print(service.fetch_inbox(_cli_principal(args), args.consumer_id, limit=args.limit))
    return 0


def cmd_memory_inbox_ack(args: argparse.Namespace) -> int:
    service = MemoryService(args.state, enable_delivery=True)
    _print(service.acknowledge_inbox(_cli_principal(args), args.consumer_id, args.cursor))
    return 0


def cmd_memory_wakeup(args: argparse.Namespace) -> int:
    service = MemoryService(args.state, enable_delivery=True, hub_store=HubStore(args.state))
    _print(service.wakeup(_cli_principal(args), args.consumer_id, budget_bytes=args.budget_bytes))
    return 0


def cmd_memory_timeline(args: argparse.Namespace) -> int:
    pair = tuple(args.pair.split(",", 1)) if args.pair and "," in args.pair else None
    _print({"items": MemoryService(args.state).timeline(
        _cli_principal(args), project=args.project, pair=pair, about=args.about,
        include_superseded=args.include_superseded, limit=args.limit,
    )})
    return 0


def cmd_memory_graph(args: argparse.Namespace) -> int:
    _print(MemoryService(args.state).graph(_cli_principal(args), args.node, hops=args.hops))
    return 0


def cmd_memory_stats(args: argparse.Namespace) -> int:
    _print(MemoryService(args.state).stats(_cli_principal(args)))
    return 0


def _run_operation(action) -> int:
    try:
        _print(action())
        return 0
    except OperationsError as exc:
        _print({"ok": False, "error": {"code": type(exc).__name__, "message": str(exc)}})
        return 2


def cmd_operations_diagnostics(args: argparse.Namespace) -> int:
    return _run_operation(lambda: OperationsDiagnostics(args.state).collect(_cli_principal(args)))


def cmd_operations_backup_create(args: argparse.Namespace) -> int:
    return _run_operation(lambda: BackupManager(args.state).create(
        args.destination,
        auth_config=args.auth_config,
        target_class=args.target_class,
        allow_sensitive_public=args.allow_sensitive_public,
    ))


def cmd_operations_backup_restore(args: argparse.Namespace) -> int:
    return _run_operation(lambda: BackupManager.restore(args.archive, args.target_state))


def cmd_operations_retention_trash_note(args: argparse.Namespace) -> int:
    return _run_operation(lambda: RetentionManager(args.state).trash_note(
        args.note_id, _cli_principal(args), allow_private=args.allow_private,
    ))


def cmd_operations_retention_trash_artifact(args: argparse.Namespace) -> int:
    return _run_operation(lambda: RetentionManager(args.state).trash_artifact(
        args.artifact_id, _cli_principal(args), allow_private=args.allow_private,
    ))


def cmd_operations_retention_restore(args: argparse.Namespace) -> int:
    return _run_operation(lambda: RetentionManager(args.state).restore(
        args.kind, args.object_id, _cli_principal(args),
    ))


def cmd_operations_retention_list(args: argparse.Namespace) -> int:
    return _run_operation(lambda: {"items": RetentionManager(args.state).list_tombstones()})


def _load_queries(path: str) -> list[dict[str, Any]]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(value, dict):
        value = value.get("queries")
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise OperationsError("migration query file must contain a queries array")
    return value


def cmd_operations_search_migrate(args: argparse.Namespace) -> int:
    return _run_operation(lambda: SearchMigrationManager(args.state).drill(
        args.server_url,
        queries=_load_queries(args.queries),
        cache_dir=args.search_cache_dir,
        parity_min=args.parity_min,
        activate=args.activate,
    ))


def cmd_operations_search_rollback(args: argparse.Namespace) -> int:
    return _run_operation(lambda: SearchMigrationManager(args.state).rollback())


def cmd_skill_path(args: argparse.Namespace) -> int:
    _print({"skill": "operate-a2a-superhub", "path": str(skill_source_path())})
    return 0


def cmd_skill_validate(args: argparse.Namespace) -> int:
    result = validate_skill()
    _print(result)
    return 0 if result["valid"] else 2


def cmd_skill_install(args: argparse.Namespace) -> int:
    try:
        result = install_skill(target=args.target, target_root=args.target_root, force=args.force)
    except SkillInstallError as exc:
        _print({"installed": False, "error": str(exc)})
        return 2
    _print(result)
    return 0


def cmd_skill_uninstall(args: argparse.Namespace) -> int:
    try:
        result = uninstall_skill(target=args.target, target_root=args.target_root)
    except SkillInstallError as exc:
        _print({"removed": False, "error": str(exc)})
        return 2
    _print(result)
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
    p.add_argument("--enable-memory", action="store_true", help="Enable the opt-in memory foundation endpoints")
    p.add_argument("--enable-delivery", action="store_true", help="Enable durable memory inbox delivery")
    p.add_argument("--enable-task-log", action="store_true", help="Enable terminal task-log sedimentation")
    p.add_argument("--enable-watcher-side-effects", action="store_true", help="Allow local-admin missing-ID repair")
    p.add_argument("--task-log-intent", action="append", help="Allowlisted task intent for sedimentation")
    p.add_argument("--principals", help="Static bearer-token to principal JSON registry")
    p.add_argument("--search-mode", choices=["keyword", "local", "server", "configured"], default="keyword")
    p.add_argument("--search-url", help="Explicit Qdrant URL for server search mode")
    p.add_argument("--search-cache-dir", help="FastEmbed model cache directory")
    p.add_argument("--enable-derivers", action="store_true", help="Enable optional PDF text and image OCR derivation; requires --enable-memory")
    p.add_argument("--max-artifact-bytes", type=int, default=64 * 1024 * 1024, help="Maximum accepted raw or resumable artifact size")
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
    p.add_argument("--visibility", default="private", help="private, shared, or direct:<principal>")
    p.set_defaults(func=cmd_artifact_put)
    p = artifact.add_parser("get", help="Get artifact manifest or content")
    p.add_argument("artifact_id")
    p.add_argument("--content", action="store_true")
    p.add_argument("--output")
    p.set_defaults(func=cmd_artifact_get)
    p = artifact.add_parser("list", help="List artifacts")
    p.set_defaults(func=cmd_artifact_list)
    p = artifact.add_parser("derive", help="Extract untrusted searchable text from a PDF or image artifact")
    p.add_argument("artifact_id")
    p.add_argument("--retry", action="store_true", help="Explicitly retry a failed or canceled derivation")
    p.set_defaults(func=cmd_artifact_derive)
    p = artifact.add_parser("derivation-status", help="Show durable artifact derivation status")
    p.add_argument("job_id")
    p.set_defaults(func=cmd_artifact_derivation_status)
    p = artifact.add_parser("derivation-purge", help="Delete a derived note and rebuildable indexes without deleting its source artifact")
    p.add_argument("job_id")
    p.set_defaults(func=cmd_artifact_derivation_purge)

    memory = sub.add_parser("memory", help="Opt-in memory foundation commands").add_subparsers(dest="memory_command", required=True)
    note = memory.add_parser("note", help="Memory note commands").add_subparsers(dest="note_command", required=True)
    p = note.add_parser("create", help="Create a Markdown memory note")
    p.add_argument("--file", required=True)
    p.add_argument("--idempotency-key")
    p.set_defaults(func=cmd_memory_note_create)
    p = note.add_parser("read", help="Read an authorized memory note")
    p.add_argument("note_id")
    p.add_argument(
        "--include-lifecycle",
        action="store_true",
        help="Include authorized stored, indexed, delivery, ACK, and linked-reference facts",
    )
    p.set_defaults(func=cmd_memory_note_read)
    p = memory.add_parser("reindex", help="Rebuild derived memory indexes from Markdown")
    p.set_defaults(func=cmd_memory_reindex)
    p = memory.add_parser("search", help="Search authorized memory notes")
    p.add_argument("query")
    p.add_argument("--mode", choices=["auto", "hybrid", "keyword"], default="auto")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--search-mode", choices=["keyword", "local", "server", "configured"], default="keyword")
    p.add_argument("--search-url")
    p.add_argument("--search-cache-dir")
    p.set_defaults(func=cmd_memory_search)
    p = memory.add_parser("search-reindex", help="Build or resume the derived hybrid index")
    p.add_argument("--search-mode", choices=["local", "server"], default="local")
    p.add_argument("--search-url")
    p.add_argument("--search-cache-dir")
    p.set_defaults(func=cmd_memory_search_reindex)
    inbox = memory.add_parser("inbox", help="Durable inbox commands").add_subparsers(dest="inbox_command", required=True)
    p = inbox.add_parser("fetch", help="Fetch without acknowledging")
    p.add_argument("--consumer-id", required=True)
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_memory_inbox_fetch)
    p = inbox.add_parser("ack", help="Acknowledge an issued cursor")
    p.add_argument("--consumer-id", required=True)
    p.add_argument("--cursor", required=True)
    p.set_defaults(func=cmd_memory_inbox_ack)
    p = memory.add_parser("wakeup", help="Build a bounded untrusted wakeup pack")
    p.add_argument("--consumer-id", required=True)
    p.add_argument("--budget-bytes", type=int, default=65536)
    p.set_defaults(func=cmd_memory_wakeup)
    p = memory.add_parser("timeline", help="Read an authorized memory timeline")
    p.add_argument("--project")
    p.add_argument("--pair")
    p.add_argument("--about")
    p.add_argument("--include-superseded", action="store_true")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_memory_timeline)
    p = memory.add_parser("graph", help="Read an authorized 1-2 hop graph")
    p.add_argument("--node", required=True)
    p.add_argument("--hops", type=int, choices=[1, 2], default=1)
    p.set_defaults(func=cmd_memory_graph)
    p = memory.add_parser("stats", help="Print sanitized memory operational stats")
    p.set_defaults(func=cmd_memory_stats)

    operations = sub.add_parser(
        "operations", help="Diagnose, back up, restore, migrate, or apply recoverable retention"
    ).add_subparsers(dest="operations_command", required=True)
    p = operations.add_parser("diagnostics", help="Print payload-free state, queue, version, and resource diagnostics")
    p.set_defaults(func=cmd_operations_diagnostics)

    backup = operations.add_parser("backup", help="Create or restore an authoritative state backup").add_subparsers(dest="backup_command", required=True)
    p = backup.add_parser("create", help="Create an integrity-manifested backup while the hub is stopped")
    p.add_argument("--destination", required=True)
    p.add_argument("--auth-config", help="Principal registry JSON to include at config/principals.json")
    p.add_argument("--target-class", choices=["private", "public"], default="private")
    p.add_argument("--allow-sensitive-public", action="store_true", help="Explicitly record and allow sensitive state in a public-classified backup")
    p.set_defaults(func=cmd_operations_backup_create)
    p = backup.add_parser("restore", help="Verify and restore into a nonexistent clean state directory")
    p.add_argument("--archive", required=True)
    p.add_argument("--target-state", required=True)
    p.set_defaults(func=cmd_operations_backup_restore)

    retention = operations.add_parser("retention", help="Move authoritative objects to recoverable trash or restore them").add_subparsers(dest="retention_command", required=True)
    p = retention.add_parser("trash-note", help="Trash an acknowledged memory note without hard deletion")
    p.add_argument("note_id")
    p.add_argument("--allow-private", action="store_true", help="Explicitly allow retention of private/direct content")
    p.set_defaults(func=cmd_operations_retention_trash_note)
    p = retention.add_parser("trash-artifact", help="Trash an unreferenced artifact manifest while retaining its blob")
    p.add_argument("artifact_id")
    p.add_argument("--allow-private", action="store_true", help="Explicitly allow retention of private/direct content")
    p.set_defaults(func=cmd_operations_retention_trash_artifact)
    p = retention.add_parser("restore", help="Restore a tombstoned memory note or artifact")
    p.add_argument("kind", choices=["memory-note", "artifact"])
    p.add_argument("object_id")
    p.set_defaults(func=cmd_operations_retention_restore)
    p = retention.add_parser("list", help="List sanitized tombstone status")
    p.set_defaults(func=cmd_operations_retention_list)

    migration = operations.add_parser("search-migration", help="Drill Qdrant local-to-server parity and rollback").add_subparsers(dest="migration_command", required=True)
    p = migration.add_parser("drill", help="Rebuild both providers and require query parity before optional activation")
    p.add_argument("--server-url", required=True)
    p.add_argument("--queries", required=True, help="JSON file containing sanitized parity queries")
    p.add_argument("--search-cache-dir")
    p.add_argument("--parity-min", type=float, default=1.0)
    p.add_argument("--activate", action="store_true", help="Record server mode only after the parity gate passes")
    p.set_defaults(func=cmd_operations_search_migrate)
    p = migration.add_parser("rollback", help="Restore the previously recorded search provider mode")
    p.set_defaults(func=cmd_operations_search_rollback)

    skill = sub.add_parser("skill", help="Discover, validate, install, or remove the product Skill").add_subparsers(dest="skill_command", required=True)
    p = skill.add_parser("path", help="Print the canonical packaged Skill path")
    p.set_defaults(func=cmd_skill_path)
    p = skill.add_parser("validate", help="Validate Skill structure and contract fingerprint")
    p.set_defaults(func=cmd_skill_validate)
    p = skill.add_parser("install", help="Install the product Skill into a supported agent runtime")
    p.add_argument("--target", choices=["codex"], required=True)
    p.add_argument("--target-root", help="Absolute Codex home override")
    p.add_argument("--force", action="store_true", help="Create a recoverable backup before replacement")
    p.set_defaults(func=cmd_skill_install)
    p = skill.add_parser("uninstall", help="Remove only files owned by this Skill installer")
    p.add_argument("--target", choices=["codex"], required=True)
    p.add_argument("--target-root", help="Absolute Codex home override")
    p.set_defaults(func=cmd_skill_uninstall)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except HubMemoryError as exc:
        error: dict[str, Any] = {
            "code": str(getattr(exc, "code", "invalid_request")).upper(),
            "message": str(exc),
            "retryable": False,
        }
        details = getattr(exc, "details", None)
        if isinstance(details, dict):
            error["details"] = details
        _print({"error": error, "traceId": f"trace_{uuid.uuid4().hex}"})
        return 2
