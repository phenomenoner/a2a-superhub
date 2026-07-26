# Memory sharing v2 compatibility and state migration

Memory sharing v2 changes the delivery read model while preserving immutable
memory notes and a bounded v1 compatibility window.

## Why the delivery model changed

A single note can match several routing rules for the same recipient. In v1,
each matched reason (`about`, `direct`, or `handoff`) was a separate inbox row.
That made one content item appear more than once and made a page cursor describe
storage rows instead of the content actually delivered.

In v2:

- delivery identity is the opaque hash of `(note ID, recipient)`;
- all matched reasons are returned as one bounded `reasons` array;
- an inbox cursor is recorded with the exact delivery IDs returned on that
  page; and
- ACK receipts are created only for those page members.

The consumer watermark remains monotonic. It may pass a delivery whose note is
no longer authorized, but such a hidden delivery never receives an ACK receipt.

## Wakeup is a preview, not ACK authority

The bounded wakeup response never contains an acknowledgeable cursor. It
reports section-level `hasMore` signals and a `read-inbox` next action when its
byte budget or inbox page limit omits data.

Only `GET /v2/memory/inbox` issues a cursor that
`POST /v2/memory/inbox/ack` can acknowledge. A client should ACK that cursor
only after the corresponding page has been accepted by the intended consumer.
Session startup, preview assembly, transport failure, or process restart never
changes durable unread state by itself.

## Ops schema v4

Ops schema v4 keeps the v1 reason rows and adds:

- one logical delivery row per note and recipient;
- the complete reason set;
- aliases from every legacy row to its logical delivery;
- a first-routing snapshot, including an empty first result;
- exact issued-cursor page membership; and
- per-consumer logical ACK receipts.

Migration groups legacy rows by note and recipient. The logical sequence is the
maximum sequence in that group. Consequently:

- a watermark below the group is unread;
- a watermark at or above the group maximum is acknowledged; and
- a watermark inside the group is conservatively redelivered.

The migrator refuses an ops schema newer than it understands. It does not
silently rewrite the schema version.

## Cursor compatibility

Schema v3 did not record whether an issued cursor came from an inbox page,
wakeup response, or ACK response. After migration, such a cursor has an
`unknown` purpose:

- a retry that would not advance the existing watermark is an idempotent no-op;
- a retry that would advance the watermark fails with
  `CURSOR_REFRESH_REQUIRED`; the caller must fetch a new exact inbox page.

This preserves safe retries without guessing that historical cursors are inbox
authority.

## v1 window and rollback boundary

Version 0.3.x continues to write and serve the v1 per-reason delivery rows.
They are a compatibility projection and audit ledger, not the v2 identity
model. The earliest version that may remove this compatibility is 0.4.0, with a
separate migration notice.

Dual-writing v1 and v2 delivery rows does not make an older binary understand
schema v4:

- before the first v4 write, rollback means restoring the verified
  pre-upgrade v3 backup and then starting the previous binary;
- after v4 writes, recovery is roll-forward with a compatible binary.

An older binary must never be pointed at a v4 state directory.

## Other version boundaries

- Stored Markdown notes continue to use
  `a2a-superhub.memory.note.v1`; existing notes are not rewritten.
- New v2 writes require relation targets in one of six namespaces:
  `agent:`, `note:`, `project:`, `task:`, `event:`, or `artifact:`.
- Lifecycle output is a set of authorized facts—stored, indexed, queued,
  acknowledged, and linked references—not a linear state and never a claim
  that a receiver understood or executed the content.
