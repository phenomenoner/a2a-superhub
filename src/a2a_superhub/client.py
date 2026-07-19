from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen


class HubClientError(RuntimeError):
    """A sanitized transport or API failure with machine-readable classification."""

    def __init__(self, message: str, *, kind: str = "protocol", status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.code = code


class HubCapabilityError(HubClientError):
    pass


def _validated_base_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise HubClientError("target must be an explicit http(s) URL without embedded credentials")
    if parsed.query or parsed.fragment:
        raise HubClientError("target URL must not contain query or fragment components")
    return value.rstrip("/") + "/"


@dataclass(frozen=True)
class HubClient:
    base_url: str = "http://127.0.0.1:8787"
    token: str | None = None
    timeout: float = 10.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", _validated_base_url(self.base_url))

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        authenticated: bool = True,
    ) -> Any:
        relative = path.lstrip("/")
        url = urljoin(self.base_url, relative)
        if query:
            url += "?" + urlencode({key: value for key, value in query.items() if value is not None})
        headers = {"Accept": "application/json"}
        if authenticated and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=payload, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            try:
                error_body = json.loads(exc.read().decode("utf-8"))
                detail = error_body.get("error")
                if isinstance(detail, dict):
                    message = str(detail.get("code") or "HTTP_ERROR")
                elif isinstance(detail, str):
                    message = detail
                else:
                    message = "HTTP_ERROR"
            except Exception:
                message = "HTTP_ERROR"
            kind = "auth" if exc.code in {401, 403} else "not-found" if exc.code == 404 else "http"
            raise HubClientError(
                f"hub request failed ({exc.code} {message})",
                kind=kind,
                status=exc.code,
                code=message,
            ) from None
        except (URLError, TimeoutError, OSError) as exc:
            raise HubClientError(f"hub connection failed ({type(exc).__name__})", kind="connection") from None
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HubClientError("hub returned an invalid JSON response") from None

    def health(self) -> dict[str, Any]:
        return self.request("GET", "/healthz", authenticated=False)

    def ready(self) -> dict[str, Any]:
        return self.request("GET", "/readyz", authenticated=False)

    def negotiate(self) -> dict[str, Any]:
        try:
            result = self.request("GET", "/v1/capabilities")
        except HubClientError as current_error:
            if current_error.status != 404:
                raise
            try:
                card = self.request("GET", "/.well-known/agent-card.json", authenticated=False)
            except HubClientError:
                raise current_error
            capabilities = card.get("capabilities") if isinstance(card, dict) else None
            if not isinstance(capabilities, dict):
                raise HubCapabilityError("server exposes neither current capabilities nor a legacy Agent Card")
            return {
                "schema": "a2a-superhub.capabilities.legacy-agent-card",
                "compatibility": "n-1-read-only",
                "memoryFoundation": bool(capabilities.get("memoryFoundation")),
            }
        if not isinstance(result, dict) or result.get("schema") != "a2a-superhub.capabilities.v1":
            raise HubCapabilityError("unsupported capabilities response")
        result = dict(result)
        result["compatibility"] = "current"
        return result

    def search(self, query: str, *, limit: int = 50, mode: str = "auto") -> dict[str, Any]:
        return self.request("GET", "/v1/memory/search", query={"q": query, "limit": limit, "mode": mode})

    def read_note(self, note_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/memory/notes/{note_id}")

    def inbox(self, consumer_id: str, *, limit: int = 100) -> dict[str, Any]:
        return self.request("GET", "/v1/memory/inbox", query={"consumerId": consumer_id, "limit": limit})

    def wakeup(self, consumer_id: str, *, budget_bytes: int = 65_536) -> dict[str, Any]:
        return self.request("GET", "/v1/memory/wakeup", query={"consumerId": consumer_id, "budgetBytes": budget_bytes})

    def ack_inbox(self, consumer_id: str, cursor: str) -> dict[str, Any]:
        return self.request("POST", "/v1/memory/inbox/ack", body={"consumerId": consumer_id, "cursor": cursor})

    def create_note(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return self.request("POST", "/v1/memory/notes", body=request, idempotency_key=idempotency_key)

    def timeline(
        self,
        *,
        project: str | None = None,
        pair: str | None = None,
        about: str | None = None,
        include_superseded: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self.request(
            "GET",
            "/v1/memory/timeline",
            query={
                "project": project,
                "pair": pair,
                "about": about,
                "includeSuperseded": str(include_superseded).lower(),
                "limit": limit,
            },
        )

    def graph(self, node: str, *, hops: int = 1) -> dict[str, Any]:
        return self.request("GET", "/v1/memory/graph", query={"node": node, "hops": hops})

    def create_task(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/v1/tasks", body=request)

    def task_status(self, task_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/tasks/{task_id}")
