from __future__ import annotations

import base64
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .artifacts import ArtifactStore
from .auth import BearerAuth, FixedWindowLimiter
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


def make_server(
    state_dir: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    token: str | None = None,
    rate_limit: int = 120,
) -> ThreadingHTTPServer:
    store = HubStore(state_dir)
    artifacts = ArtifactStore(state_dir)
    store.init()
    artifacts.init()
    auth = BearerAuth(token)
    limiter = FixedWindowLimiter(rate_limit)

    class Handler(BaseHTTPRequestHandler):
        server_version = "a2a-superhub/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _client_key(self) -> str:
            return self.client_address[0] if self.client_address else "unknown"

        def _check_auth(self) -> bool:
            if not limiter.allow(self._client_key()):
                self._json({"error": "rate limit exceeded"}, status=HTTPStatus.TOO_MANY_REQUESTS)
                return False
            result = auth.check(self.headers.get("Authorization"))
            if not result.ok:
                self._json({"error": result.reason}, status=HTTPStatus.UNAUTHORIZED)
                return False
            return True

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
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
                    self._json(HUB_AGENT_CARD)
                    return
                if not self._check_auth():
                    return
                if path == "/v1/agents":
                    self._json({"agents": store.list_agents()})
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
            except KeyError as exc:
                self._json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
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

    return ThreadingHTTPServer((host, port), Handler)


def run_server(state_dir: str | Path, *, host: str = "127.0.0.1", port: int = 8787, token: str | None = None) -> None:
    httpd = make_server(state_dir, host=host, port=port, token=token)
    print(f"a2a-superhub listening on http://{host}:{httpd.server_port}")
    httpd.serve_forever()
