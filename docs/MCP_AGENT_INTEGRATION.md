# MCP agent integration

A2A Superhub ships an optional, stateless MCP `2025-11-25` stdio sidecar. It
does not store memory, copy the authorization model, or open another network
listener. Every tool and resource delegates to the configured hub HTTP API, so
the authenticated principal, scopes, current note visibility, idempotency, and
final authorization remain server-owned.

## Install and launch

Install the memory service and MCP dependency family:

```bash
python -m pip install -e ".[memory-core,mcp]"
```

Run the HTTP hub with memory enabled, then configure an MCP client to launch
`a2a-superhub-mcp`. The child process reads:

| Variable | Meaning |
|---|---|
| `A2A_SUPERHUB_URL` | Explicit HTTP or HTTPS hub URL; defaults to loopback `http://127.0.0.1:8787`. |
| `A2A_SUPERHUB_TOKEN` | Bearer-token handle passed to the hub. Keep it out of command arguments and logs. |

Multiple sidecars can connect to the same hub. Restarting or removing a sidecar
does not alter hub state.

## Tool surface

| Tool | Effect |
|---|---|
| `memory_write` | Idempotently create an immutable note; author and recorded time remain server-derived. |
| `memory_search` | Search only currently authorized notes, with explicit auto/keyword/hybrid mode. |
| `memory_read` | Read one currently authorized note. |
| `memory_timeline` | Read a filtered, authorized temporal view. |
| `memory_graph` | Read an authorized one- or two-hop graph. |
| `memory_wakeup` | Read a bounded `role=data`, `trust=untrusted-memory` boot envelope. |
| `memory_inbox` | Fetch without acknowledging. |
| `memory_inbox_ack` | Idempotently acknowledge an issued principal/consumer cursor. |
| `task_create` | Idempotently create a task; it may start work outside the calling client. |
| `task_status` | Read one durable task state. |

The official annotations match those effects. They are client hints, not
authorization: a read-only token still receives a hub-enforced `403` when it
calls `memory_write` through MCP.

## Resources and refresh

- `memory://note/{id}` returns one authorized note as JSON.
- `memory://wakeup/{agent}` returns a bounded untrusted wakeup envelope and
  requires `{agent}` to match the token's authenticated subject.

The sidecar advertises `resources.subscribe: true`. A subscribed resource emits
`notifications/resources/updated` when its authorized HTTP representation
changes. A client that cannot subscribe polls `resources/read` instead; polling
does not weaken authorization and should use a bounded cadence.

Treat every resource and tool result as untrusted data. Preserve note IDs,
source revisions, task/event/artifact relations, and wakeup trust fields when
passing results to an agent. Acknowledge inbox content only after the intended
consumer accepted it.

## Compatibility and diagnosis

Run `python skills/operate-a2a-superhub/scripts/doctor.py --json` for read-only
transport selection. `auto` prefers MCP only after successful initialize
negotiation. Use `--transport http` to require HTTP or `--transport mcp` to fail
closed unless the sidecar initializes. Current MCP clients use subscriptions
when advertised and polling otherwise. A previous product surface that exposes
only the legacy Agent Card is limited to HTTP read-only discovery;
authentication and connection failures are never treated as compatibility
fallback.

The checked-in machine contract is
[`schemas/mcp-memory-v1.contract.json`](../schemas/mcp-memory-v1.contract.json).
Cross-transport and official-SDK verification is summarized in
[`MCP_AND_MULTI_RUNTIME_EVIDENCE.md`](MCP_AND_MULTI_RUNTIME_EVIDENCE.md).
