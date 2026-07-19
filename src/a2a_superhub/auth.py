from __future__ import annotations

import time
import hmac
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    reason: str = "ok"
    principal: "Principal | None" = None


@dataclass(frozen=True)
class Principal:
    subject: str
    kind: str
    token_id: str
    scopes: frozenset[str]

    def has(self, scope: str) -> bool:
        return scope in self.scopes or "hub.admin" in self.scopes


ALL_LEGACY_SCOPES = frozenset(
    {"task.read", "task.write", "artifact.read", "artifact.write", "memory.read", "memory.write", "memory.share", "memory.admin", "hub.admin"}
)
ALLOWED_PRINCIPAL_KINDS = {"agent", "human", "service", "operator"}
ALLOWED_SCOPES = ALL_LEGACY_SCOPES
SUBJECT_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
TOKEN_ID_PATTERN = re.compile(r"^tok_[a-z0-9][a-z0-9_-]{2,63}$")


class BearerAuth:
    """Optional bearer-token auth for local and private deployments."""

    def __init__(self, token: str | None = None, principals: dict[str, dict[str, Any]] | None = None):
        self.token = token
        self.principals = principals or {}
        for raw_token, configured in self.principals.items():
            if not isinstance(raw_token, str) or not raw_token:
                raise ValueError("principal registry tokens must be non-empty strings")
            if not isinstance(configured, dict):
                raise ValueError("principal registry entries must be objects")
            subject = configured.get("subject")
            kind = configured.get("kind", "agent")
            token_id = configured.get("tokenId", "tok_static")
            scopes = configured.get("scopes")
            if not isinstance(subject, str) or not SUBJECT_PATTERN.fullmatch(subject):
                raise ValueError("principal registry contains an invalid subject")
            if kind not in ALLOWED_PRINCIPAL_KINDS:
                raise ValueError("principal registry contains an invalid kind")
            if not isinstance(token_id, str) or not TOKEN_ID_PATTERN.fullmatch(token_id):
                raise ValueError("principal registry contains an invalid tokenId")
            if not isinstance(scopes, list) or not scopes or len(set(scopes)) != len(scopes) or not set(scopes) <= ALLOWED_SCOPES:
                raise ValueError("principal registry contains invalid scopes")

    def check(self, header_value: str | None) -> AuthResult:
        if header_value and header_value.startswith("Bearer "):
            raw_token = header_value[7:]
            configured = None
            for candidate, candidate_config in self.principals.items():
                if hmac.compare_digest(raw_token.encode("utf-8"), candidate.encode("utf-8")):
                    configured = candidate_config
            if configured is not None:
                principal = Principal(
                    subject=str(configured["subject"]),
                    kind=str(configured.get("kind", "agent")),
                    token_id=str(configured.get("tokenId", "tok_static")),
                    scopes=frozenset(configured.get("scopes") or []),
                )
                return AuthResult(True, principal=principal)
        if not self.token and not self.principals:
            return AuthResult(
                True,
                principal=Principal("local.operator", "operator", "tok_local", ALL_LEGACY_SCOPES),
            )
        expected = f"Bearer {self.token}"
        if self.token is not None and header_value is not None and hmac.compare_digest(header_value.encode("utf-8"), expected.encode("utf-8")):
            return AuthResult(
                True,
                principal=Principal("local.operator", "operator", "tok_legacy", ALL_LEGACY_SCOPES),
            )
        return AuthResult(False, "missing or invalid bearer token")


class FixedWindowLimiter:
    """Tiny in-memory limiter for the public MVP server."""

    def __init__(self, limit: int = 120, window_seconds: int = 60):
        self.limit = max(1, limit)
        self.window_seconds = max(1, window_seconds)
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.time()
        bucket = self._events[key]
        while bucket and now - bucket[0] > self.window_seconds:
            bucket.popleft()
        if len(bucket) >= self.limit:
            return False
        bucket.append(now)
        return True
