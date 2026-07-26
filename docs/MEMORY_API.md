# Memory API v2 contract

Memory v2 is the current agent-facing contract in A2A Superhub 0.3.0. It adds
logical deliveries, inbox-only ACK authority, cursor-free wakeup previews,
typed relation targets, safe typed errors, and an authorized lifecycle
projection. Stored Markdown notes continue to use
`a2a-superhub.memory.note.v1`; the upgrade does not rewrite note files.

The machine authorities are `schemas/memory-api-v2.schema.json` for the v2
shapes and `schemas/memory-api-v1.schema.json` for the shared note and
provenance definitions. Canonical examples live under
`tests/contracts/fixtures/api/`. The delivery migration, v1 compatibility
window, and state rollback rules are described in
[Memory sharing v2 compatibility and state migration](MEMORY_V2_COMPATIBILITY.md).

The memory foundation remains opt-in. The reference adapter, operator Skill,
hybrid retrieval provider, and MCP stdio sidecar are implemented. A complete
A2A 1.0 runtime binding remains absent.

## Common rules

- Authenticate before parsing an acting author. `author`, `recordedAt`, and the
  acting principal are server-derived and are not accepted in create bodies.
- Reject a non-loopback bind without an auth registry. Loopback no-token mode
  resolves to the documented local principal and fixed scopes.
- Limit HTTP JSON to 1 MiB, note body UTF-8 bytes to 256 KiB, title to 256 code
  points, tags to 32, relations to 128, and search results to 100. JSON Schema
  expresses structural and code-point limits; byte boundaries are executable
  checks.
- Scope idempotency keys to `(principal, operation, idempotencyKey)`. Replaying
  an identical canonical request returns the original result; a different hash
  returns `409 IDEMPOTENCY_CONFLICT`.
- Keep `recordedAt` server-only. Treat caller-supplied `occurredAt` as asserted
  time.
- Return `sourceRevision`, `indexedRevision`, consistency, and degraded reasons
  on indexed reads. Candidate filtering never replaces final authorization
  against current authoritative metadata.
- Treat note bodies, wakeup sections, task data, peer messages, and
  artifact-derived text as untrusted data.

## Current routes

| Method and route | Required scope | Contract |
|---|---|---|
| `GET /v2/capabilities` | authenticated read | Reports the v2 memory contract, logical delivery model, inbox-only ACK source, cursor-free wakeup mode, lifecycle availability, principal, and granular runtime features. |
| `POST /v2/memory/notes` | `memory.write`; plus `memory.share` for shared/direct | Immutable create with `Idempotency-Key`; typed relation targets are required; durable content and job precede `201`. |
| `GET /v2/memory/notes/{id}` | `memory.read` | Current visibility is final-authorized before content hydration; optional `includeLifecycle=true` returns `{note, lifecycle}`. |
| `GET /v2/memory/notes/{id}/lifecycle` | authorized author, recipient, or `memory.admin` | Returns authorized operational facts without inventing a linear state. |
| `GET /v2/memory/notes?limit=...` | `memory.read` | Lists authorized note summaries; limit 1–100. |
| `GET /v2/memory/search?q=...&mode=auto\|hybrid\|keyword` | `memory.read` | Dense+sparse RRF with recency, pushdown, final authorization, and keyword fallback. |
| `GET /v2/memory/inbox?consumerId=...` | `memory.read` | Returns logical deliveries and issues an exact page cursor; fetching does not acknowledge. |
| `POST /v2/memory/inbox/ack` | `memory.read` | Accepts only a previously issued inbox cursor bound to the principal and consumer; monotonic and idempotent. |
| `GET /v2/memory/wakeup?consumerId=...` | `memory.read` | Returns a bounded `role=data`, `trust=untrusted-memory` preview with no cursor and no ACK authority. |
| `GET /v2/memory/timeline` | `memory.read` | Deterministic newest-first project/pair/about temporal view with caller-visible superseders only. |
| `GET /v2/memory/graph?node=...&hops=1\|2` | `memory.read` | Final-authorized graph; unreadable note targets remove the complete edge. |
| `GET /v2/memory/stats` | `memory.admin` | Counts and degraded reasons only; no note content. |
| `GET /v1/memory/receipts?traceId=...` | `memory.admin` | Sanitized write/index/delivery/ACK operation phases; this administrative route remains versioned separately. |
| `POST /v1/memory/task-log/replay` | `memory.admin` | Replays terminal outbox entries when task-log and its intent allowlist are enabled. |

The default runtime leaves the foundation disabled and exposes no memory route.
Install `.[memory-core]` and pass `serve --enable-memory`. Delivery, task-log,
and watcher writes additionally require `--enable-delivery`,
`--enable-task-log` with one or more `--task-log-intent`, and
`--enable-watcher-side-effects`. All three default off.

## Logical delivery and exact ACK

A note can match more than one routing rule for the same recipient. V2 exposes
one logical delivery for the `(note ID, recipient)` pair:

```json
{
  "deliveryId": "del_<64-lowercase-hex>",
  "reasons": ["about", "direct", "handoff"],
  "note": {},
  "provenance": {}
}
```

`reasons` contains every applicable value from `about`, `direct`, and
`handoff`. It is bounded, unique, and deterministically ordered. The content is
not repeated merely because several reasons match.

Each inbox response records the exact logical delivery IDs returned with its
cursor. An ACK creates receipts only for those authorized page members and
advances the consumer watermark monotonically. Hidden or no-longer-authorized
deliveries may be passed by the watermark but never receive a fabricated ACK
receipt. Replaying an already-applied cursor is a safe no-op.

The cursor is bound to its issuing principal, consumer, delivery model, purpose,
and page ledger. A forged, mismatched, or unissued cursor returns
`CURSOR_INVALID`. A migrated historical cursor whose purpose was not recorded
may be replayed only when it cannot advance the watermark; otherwise the server
returns `409 CURSOR_REFRESH_REQUIRED` and the client must fetch a new inbox
page.

`consumerId` identifies a stable consumer lane such as one agent/device
integration. It is not the authenticated principal ID, although the server
binds every issued cursor to both values. Reusing the assigned ID preserves
that lane's watermark across restarts; creating a new ID creates an independent
unread view.

## Wakeup is a preview

Wakeup assembles profile, inbox, recent, and active-task sections inside a
bounded untrusted envelope. It never returns an acknowledgeable cursor,
including when every available item fits.

The response reports per-section `hasMore`, truncation reasons
(`byte-budget` and/or `item-limit`), and `nextAction: read-inbox` when more data
must be fetched. Neither successful wakeup assembly nor successful delivery of
the preview changes unread state. Only a cursor returned by an inbox response
can be ACKed, and only after that exact page was accepted by the intended
consumer.

## Typed relation targets

V2 note writes require every `relations[].target` to identify its namespace:

- `agent:<principal-id>`
- `note:mem_<32-lowercase-hex>`
- `project:<id>`
- `task:<id>`
- `event:<id>`
- `artifact:<id>`

Relation types remain the documented built-ins or an `x-` extension type. A
namespace makes a target unambiguous; it does not prove the target exists or
grant access to it. Invalid targets return safe validation details such as
`fieldPath`, `rule`, `allowedValues`, or `expected` without echoing note content
or credentials.

## Lifecycle projection

Request `GET /v2/memory/notes/{id}?includeLifecycle=true` to receive the
authorized note and lifecycle together, or use the dedicated lifecycle route.
The MCP `memory_read` tool exposes the same behavior through
`includeLifecycle`.

Lifecycle facts can describe:

- durable storage and server-derived author information visible to the caller;
- the current derived-index revision and content hash, or no indexed fact;
- logical deliveries, complete reason sets, queue time, and whether an ACK
  receipt exists; and
- currently authorized notes that link to the requested note.

These are independent facts, not a state machine. An author can see an
aggregate acknowledgement without learning recipient consumer identifiers; a
recipient can see its own consumer ACK records; an administrator can see the
authorized administrative view. Unrelated principals receive not-found
behavior. Lifecycle never asserts that content was read, understood, accepted
as instructions, executed, or completed.

## Search and consistency

Search results authorize and hydrate each returned note from authoritative
Markdown. Response-level source/index revision fields describe the last
completed convergence snapshot, so a search does not wait for an unrelated
filesystem scan still in progress. Per-item `sourceRevision` remains the
authoritative revision for that result.

A completed API write advances the snapshot immediately. A changed filesystem
note appears after its convergence cycle completes. This is an
eventual-consistency boundary, not a latency claim.

The product-level `features.memory` capability is true, while each running HTTP
instance reports granular flags and keeps `memoryFull` false rather than
implying that every optional side effect or sidecar is enabled. Hybrid search
defaults to keyword-only unless the `search` extra is installed and
`--search-mode local` or `--search-mode server --search-url URL` is explicit.
Build or resume the derived index with `memory search-reindex`; this never
replaces Markdown or the operations/ACK database.

## V1 compatibility and the state boundary

Version 0.3.x continues to write and serve `/v1/memory/*` delivery views as
per-reason rows. A v1 consumer can therefore receive the same note once for
each matched routing reason. This is a compatibility projection and audit
ledger, not the v2 delivery identity model. Removal can occur no earlier than
0.4.0 and requires a separate migration notice.

Operations schema v4 stores logical deliveries, legacy aliases, exact
issued-page membership, and logical ACK receipts. An older binary must never
open a v4 state directory:

- before the first v4 write, rollback requires restoring the verified
  pre-upgrade v3 backup and then starting the previous binary;
- after any v4 write, recovery is roll-forward with a compatible binary.

Dual-writing v1 rows does not make a v4 state safe for an older binary. See
[Memory sharing v2 compatibility and state migration](MEMORY_V2_COMPATIBILITY.md)
for migration and cursor details.

## Error envelope

HTTP failures use a typed, bounded, safe envelope:

```json
{
  "error": {
    "code": "INVALID_REQUEST",
    "message": "relation target requires a typed namespace",
    "retryable": false,
    "details": {
      "fieldPath": "relations[0].target",
      "rule": "typed-namespace",
      "allowedValues": ["agent:<id>", "artifact:<id>", "event:<id>", "note:<id>", "project:<id>", "task:<id>"]
    }
  },
  "traceId": "trace_<32-lowercase-hex>"
}
```

`details` is optional and limited to safe validation metadata:
`fieldPath`, `rule`, `allowedValues`, and `expected`. The server does not return
tokens, secrets, request bodies, note content, stack traces, or host filesystem
paths. Unexpected failures use the fixed `INTERNAL_ERROR` message.

The HTTP client preserves status, code, retryability, safe details, and trace
ID. MCP returns the same fields in its structured error, adding the client
transport `kind` and HTTP `status`. Oversized or malformed details are dropped
rather than forwarded.

Oversize input is `413 REQUEST_TOO_LARGE`; idempotency conflict is `409`;
invalid or forged cursors are `400 CURSOR_INVALID`; a historical cursor that
would advance without proven inbox purpose is `409 CURSOR_REFRESH_REQUIRED`;
missing credentials are `401`; insufficient scopes are `403`.

Admin delegation, `act_as`, arbitrary PATCH, destructive delete, federation,
and read-your-write waiting are not provided by memory v2.
