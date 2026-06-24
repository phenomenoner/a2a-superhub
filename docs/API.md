# API Overview

A2A Superhub exposes a compact JSON API plus a minimal JSON-RPC A2A facade.

## Public endpoints

These endpoints are available without bearer auth:

- `GET /healthz`
- `GET /readyz`
- `GET /.well-known/agent-card.json`

## Authenticated endpoints

If `--token` or `A2A_SUPERHUB_TOKEN` is configured, all `/v1/*` and `/a2a`
requests require `Authorization: Bearer <token>`.

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
