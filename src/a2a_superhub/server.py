from __future__ import annotations

import base64
import ipaddress
import json
import stat
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .artifacts import ArtifactStore
from .auth import BearerAuth, FixedWindowLimiter, Principal
from .memory import AuthorizationError, ConflictError, CursorError, MemoryError as HubMemoryError, MemoryService, MemoryWatcher, RequestTooLargeError
from .store import HubStore

HUB_AGENT_CARD = {
    "schema": "a2a.agent-card.v1",
    "id": "a2a-superhub",
    "name": "A2A Superhub",
    "description": "A standalone hub for durable agent-to-agent task and artifact exchange.",
    "capabilities": {
        "tasks": True,
        "events": True,
        "artifacts": True,
        "idempotency": True,
        "jsonRpc": True,
    },
    "skills": [
        {"id": "task.lifecycle", "description": "Create, inspect, update, cancel, and recover hub tasks."},
        {"id": "artifact.cas", "description": "Store and retrieve content-addressed artifacts."},
        {"id": "agent.registry", "description": "Register and list peer Agent Cards."},
    ],
}


def _event_touches_markdown(event: Any) -> bool:
    source = Path(event.src_path)
    destination_value = getattr(event, "dest_path", None)
    destination = Path(destination_value) if destination_value else None
    return source.suffix.casefold() == ".md" or bool(destination and destination.suffix.casefold() == ".md")


def _safe_markdown_snapshot(notes_root: Path, candidates: list[Any] | None = None) -> tuple[tuple[str, int, int], ...]:
    if candidates is None:
        try:
            candidates = list(notes_root.glob("**/*.md"))
        except OSError:
            return (("<scan-error>", -1, -1),)
    entries: list[tuple[str, int, int]] = []
    for path in sorted(candidates, key=str):
        try:
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode):
                continue
            relative = str(path.relative_to(notes_root))
            entries.append((relative, metadata.st_size, metadata.st_mtime_ns))
        except (FileNotFoundError, PermissionError, OSError):
            try:
                relative = str(path.relative_to(notes_root))
            except (ValueError, OSError):
                relative = str(path)
            entries.append((relative, -1, -1))
    return tuple(entries)


def make_server(
    state_dir: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    token: str | None = None,
    rate_limit: int = 120,
    enable_memory: bool = False,
    enable_delivery: bool = False,
    enable_task_log: bool = False,
    enable_watcher_side_effects: bool = False,
    task_log_intents: set[str] | frozenset[str] | None = None,
    principals: dict[str, dict[str, Any]] | None = None,
    search_mode: str = "keyword",
    search_url: str | None = None,
    search_cache_dir: str | Path | None = None,
) -> ThreadingHTTPServer:
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host.casefold() == "localhost"
    if not is_loopback and not token and not principals:
        raise ValueError("non-loopback binding requires bearer authentication")
    store = HubStore(state_dir)
    artifacts = ArtifactStore(state_dir)
    store.init()
    artifacts.init()
    auth = BearerAuth(token, principals)
    limiter = FixedWindowLimiter(rate_limit)
    search_provider = None
    if enable_memory and search_mode in {"local", "server"}:
        from .retrieval import QdrantRetrievalProvider
        search_provider = QdrantRetrievalProvider(
            state_dir, mode=search_mode, url=search_url, cache_dir=search_cache_dir,
        )
    elif search_mode != "keyword":
        raise ValueError("search mode must be keyword, local, or server")
    memory = MemoryService(
        state_dir,
        enable_delivery=enable_delivery,
        enable_task_log=enable_task_log,
        enable_watcher_side_effects=enable_watcher_side_effects,
        task_log_intents=task_log_intents,
        hub_store=store,
        search_provider=search_provider,
    ) if enable_memory else None
    if memory:
        memory.init()
    runtime_watcher_enabled = False

    class Handler(BaseHTTPRequestHandler):
        server_version = "a2a-superhub/0.1"
        _principal: Principal | None = None

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _client_key(self) -> str:
            return self.client_address[0] if self.client_address else "unknown"

        def _check_auth(self) -> bool:
            result = auth.check(self.headers.get("Authorization"))
            if not result.ok:
                self._api_error("AUTH_REQUIRED", "bearer authentication is required", HTTPStatus.UNAUTHORIZED)
                return False
            self._principal = result.principal
            limiter_key = f"{self._client_key()}:{self._principal.subject if self._principal else 'anonymous'}"
            if not limiter.allow(limiter_key):
                self._json({"error": "rate limit exceeded"}, status=HTTPStatus.TOO_MANY_REQUESTS)
                return False
            return True

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if length > 1_048_576:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 65_536))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                raise OverflowError("request too large")
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            obj = json.loads(raw.decode("utf-8"))
            if not isinstance(obj, dict):
                raise ValueError("request body must be a JSON object")
            return obj

        def _json(self, obj: Any, *, status: HTTPStatus = HTTPStatus.OK) -> None:
            data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _api_error(self, code: str, message: str, status: HTTPStatus, *, retryable: bool = False) -> None:
            self._json(
                {"error": {"code": code, "message": message, "retryable": retryable}, "traceId": f"trace_{uuid.uuid4().hex}"},
                status=status,
            )

        def _bytes(self, data: bytes, *, media_type: str = "application/octet-stream") -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                if path in {"/healthz", "/readyz"}:
                    self._json(store.health())
                    return
                if path == "/.well-known/agent-card.json":
                    card = json.loads(json.dumps(HUB_AGENT_CARD))
                    card["capabilities"]["memoryFoundation"] = bool(memory)
                    self._json(card)
                    return
                if not self._check_auth():
                    return
                if path == "/v1/agents":
                    self._json({"agents": store.list_agents()})
                    return
                if path == "/v1/capabilities":
                    self._json({
                        "schema": "a2a-superhub.capabilities.v1",
                        "memoryFoundation": bool(memory),
                        "memorySharing": bool(memory and memory.enable_delivery),
                        "timelineGraph": bool(memory),
                        "safeWakeup": bool(memory),
                        "taskLog": bool(memory and memory.enable_task_log),
                        "watcherSideEffects": bool(memory and memory.enable_watcher_side_effects),
                        "runtimeWatcher": runtime_watcher_enabled,
                        "adapter": bool(memory and memory.enable_delivery),
                        "memoryFull": False,
                        "memorySearch": "hybrid" if search_provider else "keyword",
                        "retrieval": memory.search_status() if memory else {"provider": "keyword"},
                        "principal": {
                            "subject": self._principal.subject,
                            "kind": self._principal.kind,
                            "tokenId": self._principal.token_id,
                            "scopes": sorted(self._principal.scopes),
                        },
                    })
                    return
                if memory and path in {"/v1/memory/notes", "/v1/memory/search"}:
                    if not self._principal.has("memory.read"):
                        raise AuthorizationError("memory.read scope required")
                    query = parse_qs(parsed.query)
                    requested_mode = query.get("mode", ["auto"])[0]
                    notes = memory.search(
                        query.get("q", [""])[0], self._principal,
                        limit=int(query.get("limit", ["50"])[0]), mode=requested_mode,
                    )
                    items = [
                        {
                            "id": note["id"],
                            "author": note["author"],
                            "visibility": note["visibility"],
                            "recordedAt": note["recordedAt"],
                            "sourceRevision": memory.source_revision(note["id"]),
                        }
                        for note in notes
                    ]
                    self._json({"items": items, "search": memory.search_status(), **memory.index_status()})
                    return
                if memory and path.startswith("/v1/memory/notes/"):
                    if not self._principal.has("memory.read"):
                        raise AuthorizationError("memory.read scope required")
                    note_id = path.split("/")[4]
                    self._json(memory.read_note(note_id, self._principal))
                    return
                if memory and path == "/v1/memory/inbox":
                    query = parse_qs(parsed.query)
                    self._json(memory.fetch_inbox(
                        self._principal, query.get("consumerId", [self._principal.subject])[0],
                        limit=int(query.get("limit", ["100"])[0]),
                    ))
                    return
                if memory and path == "/v1/memory/wakeup":
                    query = parse_qs(parsed.query)
                    self._json(memory.wakeup(
                        self._principal, query.get("consumerId", [self._principal.subject])[0],
                        budget_bytes=int(query.get("budgetBytes", ["65536"])[0]),
                    ))
                    return
                if memory and path == "/v1/memory/timeline":
                    query = parse_qs(parsed.query)
                    pair_raw = query.get("pair", [None])[0]
                    pair = tuple(pair_raw.split(",", 1)) if pair_raw and "," in pair_raw else None
                    self._json({"items": memory.timeline(
                        self._principal,
                        project=query.get("project", [None])[0],
                        pair=pair,
                        about=query.get("about", [None])[0],
                        include_superseded=query.get("includeSuperseded", ["false"])[0].casefold() == "true",
                        limit=int(query.get("limit", ["100"])[0]),
                    )})
                    return
                if memory and path == "/v1/memory/graph":
                    query = parse_qs(parsed.query)
                    node = query.get("node", [""])[0]
                    if not node:
                        raise HubMemoryError("graph node is required")
                    self._json(memory.graph(self._principal, node, hops=int(query.get("hops", ["1"])[0])))
                    return
                if memory and path == "/v1/memory/stats":
                    self._json(memory.stats(self._principal))
                    return
                if memory and path == "/v1/memory/receipts":
                    if not self._principal.has("memory.admin"):
                        raise AuthorizationError("memory.admin scope required")
                    query = parse_qs(parsed.query)
                    self._json({"items": memory.list_receipts(trace_id=query.get("traceId", [None])[0])})
                    return
                if path == "/v1/tasks":
                    query = parse_qs(parsed.query)
                    limit = int(query.get("limit", ["50"])[0])
                    self._json({"tasks": store.list_tasks(limit=limit)})
                    return
                if path.startswith("/v1/tasks/"):
                    parts = path.split("/")
                    task_id = parts[3]
                    if len(parts) == 5 and parts[4] == "events":
                        self._json({"events": store.list_events(task_id)})
                        return
                    task = store.get_task(task_id)
                    if not task:
                        self._json({"error": "task not found"}, status=HTTPStatus.NOT_FOUND)
                        return
                    self._json(task)
                    return
                if path == "/v1/artifacts":
                    self._json({"artifacts": artifacts.list_manifests()})
                    return
                if path.startswith("/v1/artifacts/"):
                    parts = path.split("/")
                    artifact_id = parts[3]
                    manifest = artifacts.get_manifest(artifact_id)
                    if not manifest:
                        self._json({"error": "artifact not found"}, status=HTTPStatus.NOT_FOUND)
                        return
                    if len(parts) == 5 and parts[4] == "content":
                        data = artifacts.get_bytes(artifact_id)
                        if data is None:
                            self._json({"error": "artifact blob missing"}, status=HTTPStatus.NOT_FOUND)
                            return
                        self._bytes(data, media_type=manifest.get("mediaType") or "application/octet-stream")
                        return
                    self._json(manifest)
                    return
                self._json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            except KeyError:
                self._api_error("NOT_FOUND", "resource not found", HTTPStatus.NOT_FOUND)
            except AuthorizationError:
                self._api_error("SCOPE_DENIED", "memory.read is required", HTTPStatus.FORBIDDEN)
            except CursorError as exc:
                self._api_error("CURSOR_INVALID", str(exc), HTTPStatus.BAD_REQUEST)
            except HubMemoryError as exc:
                self._api_error("INVALID_REQUEST", str(exc), HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                if not self._check_auth():
                    return
                body = self._read_json()
                if path == "/a2a":
                    self._handle_json_rpc(body)
                    return
                if path == "/v1/agents/register":
                    self._json(store.register_agent(body.get("card") or body))
                    return
                if memory and path == "/v1/memory/notes":
                    body_key = body.pop("idempotencyKey", None)
                    header_key = self.headers.get("Idempotency-Key")
                    if body_key and header_key and body_key != header_key:
                        raise ConflictError("idempotency key mismatch")
                    idempotency_key = header_key or body_key
                    if not idempotency_key:
                        raise HubMemoryError("idempotency key is required")
                    result = memory.create_note(
                        body,
                        self._principal,
                        idempotency_key=idempotency_key,
                    )
                    self._json(
                        {
                            "id": result.note["id"],
                            "author": result.note["author"],
                            "visibility": result.note["visibility"],
                            "recordedAt": result.note["recordedAt"],
                            "sourceRevision": memory.source_revision(result.note["id"]),
                            "traceId": result.trace_id,
                        },
                        status=HTTPStatus.CREATED if result.inserted else HTTPStatus.OK,
                    )
                    return
                if memory and path == "/v1/memory/inbox/ack":
                    self._json(memory.acknowledge_inbox(
                        self._principal,
                        str(body.get("consumerId") or self._principal.subject),
                        str(body.get("cursor") or ""),
                    ))
                    return
                if memory and path == "/v1/memory/task-log/replay":
                    if not self._principal.has("memory.admin"):
                        raise AuthorizationError("memory.admin scope required")
                    self._json(memory.replay_terminal_outbox(store))
                    return
                if path == "/v1/tasks":
                    task, inserted = store.create_task(body)
                    self._json({"inserted": inserted, "task": task}, status=HTTPStatus.CREATED if inserted else HTTPStatus.OK)
                    return
                if path.startswith("/v1/tasks/"):
                    parts = path.split("/")
                    task_id = parts[3]
                    if len(parts) == 5 and parts[4] == "events":
                        event = store.append_event(task_id, body.get("kind", "task.progress"), body.get("payload") or {}, state=body.get("state"))
                        self._json({"event": event})
                        return
                    if len(parts) == 5 and parts[4] == "cancel":
                        event = store.append_event(task_id, "task.canceled", body or {"reason": "cancel requested"}, state="canceled")
                        self._json({"event": event, "task": store.get_task(task_id)})
                        return
                if path == "/v1/artifacts":
                    content = body.get("contentBase64")
                    if not isinstance(content, str):
                        raise ValueError("artifact upload requires contentBase64")
                    manifest = artifacts.put_bytes(
                        base64.b64decode(content),
                        filename=body.get("filename"),
                        media_type=body.get("mediaType") or "application/octet-stream",
                        created_by=body.get("createdBy") or "unknown",
                        policy=body.get("policy") or None,
                    )
                    self._json(manifest, status=HTTPStatus.CREATED)
                    return
                self._json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            except KeyError:
                self._api_error("NOT_FOUND", "resource not found", HTTPStatus.NOT_FOUND)
            except AuthorizationError as exc:
                message = "required memory scope is missing"
                if "memory.share" in str(exc):
                    message = "memory.share is required"
                elif "memory.write" in str(exc):
                    message = "memory.write is required"
                self._api_error("SCOPE_DENIED", message, HTTPStatus.FORBIDDEN)
            except ConflictError:
                self._api_error("IDEMPOTENCY_CONFLICT", "idempotency key conflicts with an earlier request", HTTPStatus.CONFLICT)
            except CursorError as exc:
                self._api_error("CURSOR_INVALID", str(exc), HTTPStatus.BAD_REQUEST)
            except OverflowError:
                self._api_error("REQUEST_TOO_LARGE", "request body is too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            except RequestTooLargeError:
                self._api_error("REQUEST_TOO_LARGE", "note body is too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            except HubMemoryError as exc:
                self._api_error("INVALID_REQUEST", str(exc), HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

        def _handle_json_rpc(self, body: dict[str, Any]) -> None:
            request_id = body.get("id")
            method = body.get("method")
            params = body.get("params") or {}
            try:
                if method in {"message/send", "tasks/send", "tasks/create"}:
                    task, inserted = store.create_task(params)
                    result = {"inserted": inserted, "task": task}
                elif method == "tasks/get":
                    task_id = params.get("id") or params.get("taskId")
                    task = store.get_task(str(task_id)) if task_id else None
                    if not task:
                        raise KeyError("task not found")
                    result = task
                elif method == "tasks/cancel":
                    task_id = params.get("id") or params.get("taskId")
                    if not task_id:
                        raise ValueError("tasks/cancel requires id")
                    event = store.append_event(str(task_id), "task.canceled", params, state="canceled")
                    result = {"event": event, "task": store.get_task(str(task_id))}
                else:
                    self._json({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found"}}, status=HTTPStatus.BAD_REQUEST)
                    return
                self._json({"jsonrpc": "2.0", "id": request_id, "result": result})
            except Exception as exc:
                self._json({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": str(exc)}}, status=HTTPStatus.BAD_REQUEST)

    httpd = ThreadingHTTPServer((host, port), Handler)
    if memory:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            watcher = MemoryWatcher(memory)
            changed = threading.Event()
            stopped = threading.Event()
            converged = threading.Event()

            class NotesHandler(FileSystemEventHandler):
                def on_any_event(self, event: Any) -> None:
                    if event.is_directory:
                        return
                    source = Path(event.src_path)
                    destination_value = getattr(event, "dest_path", None)
                    destination = Path(destination_value) if destination_value else None
                    if not _event_touches_markdown(event):
                        return
                    watcher.notify(source, event.event_type, dest_path=destination)
                    changed.set()

            def converge() -> None:
                while not stopped.is_set():
                    if not changed.wait(0.25):
                        continue
                    changed.clear()
                    if stopped.wait(0.30):
                        return
                    try:
                        watcher.flush(force=True)
                    except Exception:
                        # The next filesystem event or explicit operator scan retries;
                        # request handlers continue to fail closed on quarantined IDs.
                        pass
                    else:
                        converged.set()

            notes_root = memory.root / "notes"
            notes_root.mkdir(parents=True, exist_ok=True)
            observer = Observer()
            observer.schedule(NotesHandler(), str(notes_root), recursive=True)
            observer.start()
            convergence_thread = threading.Thread(target=converge, name="a2a-memory-convergence", daemon=True)
            convergence_thread.start()
            original_close = httpd.server_close

            def close_with_watcher() -> None:
                stopped.set()
                changed.set()
                observer.stop()
                observer.join(timeout=5)
                convergence_thread.join(timeout=5)
                original_close()

            httpd.server_close = close_with_watcher  # type: ignore[method-assign]
            httpd.runtime_watcher_enabled = True  # type: ignore[attr-defined]
            httpd.memory_convergence_event = converged  # type: ignore[attr-defined]
            runtime_watcher_enabled = True
        except ImportError:
            watcher = MemoryWatcher(memory)
            stopped = threading.Event()
            converged = threading.Event()
            notes_root = memory.root / "notes"
            notes_root.mkdir(parents=True, exist_ok=True)

            def snapshot() -> tuple[tuple[str, int, int], ...]:
                return _safe_markdown_snapshot(notes_root)

            def poll_convergence() -> None:
                previous = snapshot()
                while not stopped.wait(0.50):
                    current = snapshot()
                    if current != previous:
                        try:
                            watcher.scan_once()
                        except Exception:
                            pass
                        else:
                            converged.set()
                        previous = current

            polling_thread = threading.Thread(target=poll_convergence, name="a2a-memory-poll-convergence", daemon=True)
            polling_thread.start()
            original_close = httpd.server_close

            def close_with_polling_watcher() -> None:
                stopped.set()
                polling_thread.join(timeout=5)
                original_close()

            httpd.server_close = close_with_polling_watcher  # type: ignore[method-assign]
            httpd.runtime_watcher_enabled = True  # type: ignore[attr-defined]
            httpd.memory_convergence_event = converged  # type: ignore[attr-defined]
            runtime_watcher_enabled = True
    return httpd


def run_server(
    state_dir: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    token: str | None = None,
    enable_memory: bool = False,
    enable_delivery: bool = False,
    enable_task_log: bool = False,
    enable_watcher_side_effects: bool = False,
    task_log_intents: set[str] | frozenset[str] | None = None,
    principals: dict[str, dict[str, Any]] | None = None,
    search_mode: str = "keyword",
    search_url: str | None = None,
    search_cache_dir: str | Path | None = None,
) -> None:
    httpd = make_server(
        state_dir, host=host, port=port, token=token, enable_memory=enable_memory,
        enable_delivery=enable_delivery, enable_task_log=enable_task_log,
        enable_watcher_side_effects=enable_watcher_side_effects,
        task_log_intents=task_log_intents, principals=principals,
        search_mode=search_mode, search_url=search_url, search_cache_dir=search_cache_dir,
    )
    print(f"a2a-superhub listening on http://{host}:{httpd.server_port}")
    httpd.serve_forever()
