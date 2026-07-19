from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .client import HubClient


class AdapterError(RuntimeError):
    pass


class CapabilityMismatchError(AdapterError):
    pass


class RoleBoundaryError(AdapterError):
    pass


class SessionAuthorizationError(AdapterError):
    pass


_LINK_ID = re.compile(r"[A-Za-z0-9._:-]{1,200}")


@dataclass
class ReferenceAdapter:
    """A removable agent-runtime adapter; the hub server never imports it."""

    client: HubClient
    principal: str
    consumer_id: str

    @staticmethod
    def _require(capabilities: dict[str, Any], *features: str) -> None:
        missing = [name for name in features if capabilities.get(name) is not True]
        if missing:
            raise CapabilityMismatchError("server does not advertise required capability: " + ", ".join(missing))

    def _verify_principal(self, capabilities: dict[str, Any]) -> None:
        identity = capabilities.get("principal")
        if not isinstance(identity, dict) or identity.get("subject") != self.principal:
            raise SessionAuthorizationError("authenticated server principal does not match adapter principal")

    @staticmethod
    def _require_scopes(capabilities: dict[str, Any], *required: str) -> None:
        identity = capabilities.get("principal")
        scopes = set(identity.get("scopes") or []) if isinstance(identity, dict) else set()
        missing = [scope for scope in required if scope not in scopes and "hub.admin" not in scopes]
        if missing:
            raise SessionAuthorizationError("authenticated principal is missing required scope: " + ", ".join(missing))

    def start_session(
        self,
        deliver: Callable[[dict[str, Any]], Any],
        *,
        context_role: str = "data",
        budget_bytes: int = 65_536,
    ) -> dict[str, Any]:
        if context_role != "data":
            raise RoleBoundaryError("wakeup context may only be delivered with role=data")
        capabilities = self.client.negotiate()
        self._require(capabilities, "memoryFoundation", "memorySharing", "safeWakeup", "adapter")
        self._verify_principal(capabilities)
        self._require_scopes(capabilities, "memory.read")
        pack = self.client.wakeup(self.consumer_id, budget_bytes=budget_bytes)
        if pack.get("role") != "data" or pack.get("trust") != "untrusted-memory":
            raise RoleBoundaryError("server wakeup envelope violates the untrusted data boundary")
        cursor = pack.get("cursor")
        if not isinstance(cursor, str) or not cursor:
            raise AdapterError("wakeup response did not include an acknowledgeable cursor")
        block = {
            "role": "data",
            "trust": "untrusted-memory",
            "content": (
                "--- BEGIN A2A SUPERHUB UNTRUSTED DATA ---\n"
                + json.dumps(pack, ensure_ascii=False, sort_keys=True)
                + "\n--- END A2A SUPERHUB UNTRUSTED DATA ---"
            ),
            "provenance": {"source": "a2a-superhub", "principal": self.principal, "consumerId": self.consumer_id},
        }
        deliver(block)
        ack = self.client.ack_inbox(self.consumer_id, cursor)
        return {"context": block, "ack": ack, "capabilities": capabilities}

    @staticmethod
    def _link(kind: str, value: str) -> dict[str, str]:
        if not isinstance(value, str) or not _LINK_ID.fullmatch(value):
            raise AdapterError(f"invalid {kind} provenance identifier")
        return {"type": f"x-source-{kind}", "target": f"{kind}:{value}"}

    def end_session(
        self,
        *,
        authorized: bool,
        recipient: str,
        title: str,
        body: str,
        idempotency_key: str,
        project: str | None = None,
        task_id: str | None = None,
        event_ids: Iterable[str] = (),
        artifact_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        if not authorized:
            raise SessionAuthorizationError("session-end handoff requires explicit authorization")
        capabilities = self.client.negotiate()
        self._require(capabilities, "memoryFoundation", "memorySharing", "adapter")
        self._verify_principal(capabilities)
        self._require_scopes(capabilities, "memory.write", "memory.share")
        if not _LINK_ID.fullmatch(recipient):
            raise AdapterError("invalid handoff recipient")
        relations: list[dict[str, str]] = []
        if task_id:
            relations.append(self._link("task", task_id))
        relations.extend(self._link("event", value) for value in event_ids)
        relations.extend(self._link("artifact", value) for value in artifact_ids)
        request: dict[str, Any] = {
            "type": "handoff",
            "title": title,
            "body": body,
            "visibility": f"direct:{recipient}",
            "participants": [self.principal, recipient],
            "about": [recipient],
            "relations": relations,
        }
        if project:
            request["project"] = project
        return self.client.create_note(request, idempotency_key)
