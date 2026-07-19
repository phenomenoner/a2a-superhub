# Memory API v1 contract

Status: **✅ MCP-integrated foundation + hybrid search (opt-in)** — the memory foundation,
reference adapter, operator Skill, and hybrid retrieval provider are implemented
behind opt-in runtime flags and explicit Skill install.
Markdown create/read/search, FTS5, durable multi-consumer inbox, timeline/graph,
safe wakeup, sanitized receipts/stats, watcher repair, and task-log sedimentation
are implemented. Hybrid retrieval is available through the `search` extra in
Qdrant local or explicit-server mode. The MCP stdio sidecar is implemented; an
A2A 1.0 runtime binding remains absent.
The machine authority is `schemas/memory-api-v1.schema.json`; canonical examples
live under `tests/contracts/fixtures/api/`.

## Common rules

- Authenticate before parsing an acting author. `author`, `recordedAt`, and the
  acting principal are server-derived and are not accepted in create bodies.
- Reject a non-loopback bind without an auth registry. Loopback no-token mode
  resolves to the explicit `local.operator` principal and fixed scopes.
- Limit HTTP JSON to 1 MiB, note body UTF-8 bytes to 256 KiB, title to 256 code
  points, tags to 32, relations to 128, and search results to 100. JSON Schema
  expresses structural/code-point limits; byte boundaries are supplemental
  executable assertions.
- Scope idempotency keys to `(principal, operation, idempotencyKey)`. Replaying
  an identical canonical request returns the original result; a different hash
  returns `409 IDEMPOTENCY_CONFLICT`.
- Keep `recordedAt` server-only. Treat caller `occurredAt` as asserted time.
- Return `sourceRevision`, `indexedRevision`, consistency, and degraded reasons
  on indexed reads. Candidate filtering never replaces final authorization
  against current authoritative metadata.

## Endpoints

| Method and route | Required scope | Contract |
|---|---|---|
| `POST /v1/memory/notes` | `memory.write`; plus `memory.share` for shared/direct | Immutable create with `Idempotency-Key`; durable content and job precede `201`. |
| `GET /v1/memory/notes/{id}` | `memory.read` | Current visibility is final-authorized before content hydration. |
| `GET /v1/memory/notes` | `memory.read` | Opaque cursor pagination; limit 1–100. |
| `GET /v1/memory/search?q=...&mode=auto|hybrid|keyword` | `memory.read` | Dense+sparse RRF with recency, pushdown, final authorization, and keyword fallback. |
| `GET /v1/memory/inbox?consumerId=...` | `memory.read` | Fetch does not acknowledge; caller can read only its own inbox. |
| `POST /v1/memory/inbox/ack` | `memory.read` | Accept only a previously issued cursor bound to principal and consumer; monotonic/idempotent. |
| `GET /v1/memory/wakeup?consumerId=...` | `memory.read` | Bounded `role=data`, `trust=untrusted-memory` envelope. |
| `GET /v1/memory/timeline` | `memory.read` | Deterministic newest-first project/pair/about temporal view with caller-visible superseders only. |
| `GET /v1/memory/graph?node=...&hops=1|2` | `memory.read` | Final-authorized graph; unreadable note targets remove the complete edge. |
| `GET /v1/memory/stats` | `memory.admin` | Counts and degraded reasons only; no note content. |
| `GET /v1/memory/receipts?traceId=...` | `memory.admin` | Sanitized write/index/delivery/ack operation phases. |
| `POST /v1/memory/task-log/replay` | `memory.admin` | Replay terminal outbox when task-log and intent allowlist are enabled. |
| `GET /v1/capabilities` | authenticated read | Structured product/schema/protocol/capability manifest. |

The default runtime leaves the foundation disabled and returns no memory route.
Install `.[memory-core]` and pass `serve --enable-memory`. Delivery, task-log,
and watcher writes additionally require `--enable-delivery`, `--enable-task-log`
with one or more `--task-log-intent`, and `--enable-watcher-side-effects`.
All three default off. The product-level `features.memory` capability is true,
while each running HTTP instance still reports granular flags and keeps
`memoryFull` false rather than implying that every optional side effect or
sidecar is enabled. Hybrid search defaults to keyword-only unless
the `search` extra is installed and `--search-mode local` or `--search-mode
server --search-url URL` is explicit. Build or resume the derived index with
`memory search-reindex`; this never replaces Markdown or the ops/ack database.

Admin delegation, `act_as`, arbitrary PATCH, destructive delete, and
read-your-write waiting are not in memory.v1.

## Error envelope

All failures use a stable code, safe message, retryability, and trace ID. Never
include a token, secret, request body, or host filesystem path.

```json
{
  "error": {
    "code": "SCOPE_DENIED",
    "message": "memory.share is required",
    "retryable": false
  },
  "traceId": "trace_safe001"
}
```

Oversize input is rejected as `413 REQUEST_TOO_LARGE`; idempotency conflict is
`409`; invalid or forged cursors are `400 CURSOR_INVALID`; missing credentials
are `401`; insufficient scopes are `403`.
