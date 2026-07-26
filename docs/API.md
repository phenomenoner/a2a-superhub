# API Overview

A2A Superhub exposes a compact JSON API plus a minimal JSON-RPC A2A facade.

This file documents the coordination runtime and opt-in durable-memory,
offline-sharing, hybrid-retrieval, and optional artifact-text derivation foundations.
Memory v2 is the current agent-facing contract; the complete route and payload
contract is in [MEMORY_API.md](MEMORY_API.md). The delivery migration,
compatibility window, and rollback boundary are in
[MEMORY_V2_COMPATIBILITY.md](MEMORY_V2_COMPATIBILITY.md). Inbox, wakeup,
task-log sedimentation, the reference adapter, operator Skill, and MCP stdio
sidecar are implemented. An A2A 1.0 runtime binding remains absent.

## Public endpoints

These endpoints are available without bearer auth:

- `GET /healthz`
- `GET /readyz`
- `GET /.well-known/agent-card.json`

## Authenticated endpoints

If `--token` or `A2A_SUPERHUB_TOKEN` is configured, all `/v1/*`, `/v2/*`, and
`/a2a` requests require `Authorization: Bearer <token>`.

Non-loopback binds fail at startup unless a legacy token or static principal
registry is configured. Loopback no-token mode resolves to `local.operator`.

## Opt-in memory and retrieval foundation

Install `.[memory-core]` and run `serve --enable-memory`. The default remains
off. New agents should negotiate `GET /v2/capabilities` and use `/v2/memory/*`.
Version 0.3.x also writes and serves the v1 per-reason delivery projection so
existing v1 consumers continue to work during the documented compatibility
window.

- `POST /v2/memory/notes` creates an immutable Markdown note. Supply a 1–128
  character idempotency key in `Idempotency-Key` or `idempotencyKey`; author,
  source, and recorded time are server-derived. Relation targets must use
  `agent:`, `note:`, `project:`, `task:`, `event:`, or `artifact:`.
- `GET /v2/memory/notes/<id>` reads only after final authorization against the
  current Markdown frontmatter. Add `includeLifecycle=true`, or request
  `/v2/memory/notes/<id>/lifecycle`, for authorized operational facts.
- `GET /v2/memory/notes?limit=...` lists authorized note summaries.
- `GET /v2/memory/search?q=...&limit=...&mode=auto|hybrid|keyword` performs
  authorized hybrid retrieval when configured and retains FTS-compatible
  keyword fallback.
- `GET /v2/capabilities` reports the `logical.v2` delivery model,
  `ackCursorSource: inbox-only`, `wakeupAckMode: none`, lifecycle availability,
  and granular runtime features. `memoryFull` remains false.

One v2 inbox item represents one logical delivery identified by note and
recipient. If several routing rules match, the item contains the complete
bounded `reasons` array (`about`, `direct`, and/or `handoff`) instead of
duplicating the content. Within v2, only `/v2/memory/inbox` issues an
acknowledgeable cursor, recorded with the exact delivery IDs returned on that
page. Wakeup is a bounded untrusted preview: it never returns a cursor and
never authorizes an ACK. Acknowledge an inbox cursor only after that exact page
was accepted by the intended consumer.

Lifecycle output is an authorization-filtered set of stored, indexed, queued,
acknowledged, and linked-reference facts. It is not a linear state and does not
claim that another agent understood or executed the note.

The same opt-in runtime provides durable multi-consumer inbox fetch/ack, safe
wakeup, timeline/graph, sanitized stats/receipts, and allowlisted task-log
replay. Errors use a typed safe envelope with `code`, `message`, `retryable`,
optional bounded validation `details`, and `traceId`; the HTTP client and MCP
sidecar preserve these fields. See [MEMORY_API.md](MEMORY_API.md) for scopes,
schemas, and compatibility behavior.

CLI support covers note create/read, reindex, inbox fetch/ack, wakeup,
timeline/graph, and stats. The separate `skill` commands expose path,
validation, contained install, and ownership-aware uninstall. Reindex builds a
new derived-index generation and atomically swaps it; it never rebuilds or
deletes the ops database.

## Operational diagnostics and local controls

`GET /v1/operations/diagnostics` requires `hub.admin` and returns the
`a2a-superhub.operations-diagnostics.v1` payload-free view: store counts,
pending queue/outbox and quarantine counts, source/index revision, retrieval
model identity, product version, and aggregate state bytes. It never returns
task payloads, note bodies, token material, or local paths. Only one full
diagnostic refresh runs at a time. While it runs, concurrent requests receive
the last completed snapshot, with its original `generatedAt` value, instead of
starting duplicate corpus scans. A caller can therefore detect an unchanged
snapshot without confusing it with newly collected state.

Authoritative backup/clean restore, recoverable retention, and Qdrant migration
are intentionally local CLI operations, not remote mutation endpoints. The
running hub holds a state lease, so these commands fail closed until it is
stopped. Their machine contracts are in `schemas/operations-v1.schema.json` and
operator procedures are in [OPERATIONS.md](OPERATIONS.md).

## MCP sidecar

Install `.[mcp]` and launch `a2a-superhub-mcp`. The sidecar reads
`A2A_SUPERHUB_URL` and `A2A_SUPERHUB_TOKEN`, speaks MCP `2025-11-25` over stdio,
and delegates every operation to these HTTP endpoints. It exposes ten tools:
`memory_write`, `memory_search`, `memory_read`, `memory_timeline`,
`memory_graph`, `memory_wakeup`, `memory_inbox`, `memory_inbox_ack`,
`task_create`, and `task_status`.

The sidecar uses memory v2. `memory_read` accepts `includeLifecycle`; inbox
items use logical deliveries and reason arrays; `memory_wakeup` remains a
cursor-free preview; and `memory_inbox_ack` accepts only a cursor issued by
`memory_inbox`.

`memory://note/{id}` and `memory://wakeup/{agent}` are authorized JSON resources.
The sidecar advertises resource subscriptions and emits updated notifications
when the underlying HTTP view changes. Clients without subscription support
poll `resources/read`. Tool annotations describe effects but never replace hub
authentication, scopes, current visibility checks, or idempotency.

## Agents

### `POST /v1/agents/register`

Registers an Agent Card. The request body may be the card itself or `{ "card": ... }`.
The card must contain `id` or `name`.

### `GET /v1/agents`

Lists registered Agent Cards.

## Tasks

### `POST /v1/tasks`

Creates a task. Required fields:

- `fromAgent`
- `toAgent`

Common fields:

- `intent`
- `idempotencyKey`
- `payload`
- `artifactRefs`
- `permissions`
- `limits`
- `correlation`

Duplicate `idempotencyKey` values return the existing task instead of inserting a
new one.

### `GET /v1/tasks`

Lists recent tasks. Optional query: `limit`.

### `GET /v1/tasks/<task-id>`

Returns a task.

### `POST /v1/tasks/<task-id>/events`

Appends an event. Body fields:

- `kind`, default `task.progress`
- `payload`, default `{}`
- `state`, optional task state update

### `GET /v1/tasks/<task-id>/events`

Lists task events.

### `POST /v1/tasks/<task-id>/cancel`

Records a cancellation event and moves the task to `canceled`.

## Artifacts

The authenticated principal is the artifact owner. Reads/lists require
`artifact.read`; writes require `artifact.write`; shared/direct visibility also
requires `artifact.share`. Details and examples are in
[ARTIFACTS_AND_DERIVATION.md](ARTIFACTS_AND_DERIVATION.md).

### `POST /v1/artifacts`

Stores a base64 artifact.

Required field:

- `contentBase64`

Optional fields:

- `filename`
- `mediaType`
- `visibility`
- `sha256`
- `policy`

Any `createdBy` field is ignored; ownership is server-derived.

### `PUT /v1/artifacts/raw`

Streams a binary artifact with `Content-Length`, `Content-Type`, optional
filename/visibility headers, and an optional expected SHA-256.

### `POST /v1/artifacts/chunks`

Creates a resumable upload. Chunk bytes use
`PUT /v1/artifacts/chunks/<upload-id>/<index>`; status uses `GET` on the upload
ID; `commit` and `cancel` are explicit POST subresources.

### `GET /v1/artifacts`

Lists artifact manifests.

### `GET /v1/artifacts/<artifact-id>`

Returns an artifact manifest.

### `GET /v1/artifacts/<artifact-id>/content`

Returns raw artifact bytes after SHA-256 verification.

### `POST /v1/artifacts/<artifact-id>/policy`

Owner/admin visibility change. Derived-note reads and search use this current
manifest as their final authorization source.

### `POST /v1/artifacts/<artifact-id>/derive`

Starts or safely replays an enabled PDF/OCR derivation. Durable status is at
`GET /v1/derivations/<job-id>`; explicit `cancel` and admin-only `purge`
subresources are available. Derived text is always untrusted data.

## JSON-RPC facade

`POST /a2a` supports JSON-RPC 2.0 requests for:

- `message/send`
- `tasks/create`
- `tasks/get`
- `tasks/cancel`

This is intentionally small and should expand by capability negotiation rather
than assuming every peer supports every protocol feature.

`message/send` validates official `text`/`raw`/`url`/`data` Part oneofs and maps
large raw Parts to private CAS references. This does not make the entire facade
A2A 1.0 compliant.
