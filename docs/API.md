# API Overview

A2A Superhub exposes a compact JSON API plus a minimal JSON-RPC A2A facade.

This file documents the coordination runtime and opt-in durable-memory,
offline-sharing, and hybrid-retrieval foundations.
The complete memory.v1 contract is in [MEMORY_API.md](MEMORY_API.md). Inbox,
wakeup, task-log sedimentation, the reference adapter, and operator Skill are
implemented; MCP and an A2A 1.0 runtime binding remain absent.

## Public endpoints

These endpoints are available without bearer auth:

- `GET /healthz`
- `GET /readyz`
- `GET /.well-known/agent-card.json`

## Authenticated endpoints

If `--token` or `A2A_SUPERHUB_TOKEN` is configured, all `/v1/*` and `/a2a`
requests require `Authorization: Bearer <token>`.

Non-loopback binds fail at startup unless a legacy token or static principal
registry is configured. Loopback no-token mode resolves to `local.operator`.

## Opt-in memory and retrieval foundation

Install `.[memory-core]` and run `serve --enable-memory`. The default remains
off, so existing v1 deployments preserve their behavior.

- `POST /v1/memory/notes` creates an immutable Markdown note. Supply a 1–128
  character idempotency key in `Idempotency-Key` or `idempotencyKey`; author,
  source, and recorded time are server-derived.
- `GET /v1/memory/notes/<id>` reads only after final authorization against the
  current Markdown frontmatter.
- `GET /v1/memory/notes?limit=...` lists authorized note summaries.
- `GET /v1/memory/search?q=...&limit=...&mode=auto|hybrid|keyword` performs
  authorized hybrid retrieval when configured and retains FTS-compatible
  keyword fallback.
- `GET /v1/capabilities` reports `memoryFoundation` separately from the still
  false full-memory capability.

The same opt-in runtime provides durable multi-consumer inbox fetch/ack, safe
wakeup, timeline/graph, sanitized stats/receipts, and allowlisted task-log
replay. See [MEMORY_API.md](MEMORY_API.md) for their scopes and schemas.

CLI support covers note create/read, reindex, inbox fetch/ack, wakeup,
timeline/graph, and stats. The separate `skill` commands expose path,
validation, contained install, and ownership-aware uninstall. Reindex builds a
new derived-index generation and atomically swaps it; it never rebuilds or
deletes the ops database.

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

### `POST /v1/artifacts`

Stores a base64 artifact.

Required field:

- `contentBase64`

Optional fields:

- `filename`
- `mediaType`
- `createdBy`
- `policy`

### `GET /v1/artifacts`

Lists artifact manifests.

### `GET /v1/artifacts/<artifact-id>`

Returns an artifact manifest.

### `GET /v1/artifacts/<artifact-id>/content`

Returns raw artifact bytes after SHA-256 verification.

## JSON-RPC facade

`POST /a2a` supports JSON-RPC 2.0 requests for:

- `message/send`
- `tasks/create`
- `tasks/get`
- `tasks/cancel`

This is intentionally small and should expand by capability negotiation rather
than assuming every peer supports every protocol feature.
