# Memory security and durability contract

Status: runtime contract for durable memory, offline sharing, the reference
adapter, operator Skill, and hybrid retrieval. MCP and an A2A 1.0 runtime
binding remain outside the implemented surface.

## Authority and truth

| Data | Authority | Rebuildable? | Required backup |
|---|---|---:|---:|
| Principal identifiers, token hashes, scopes | local auth config | no | yes |
| Tasks, events, outbox sequence | `hub-tasks.sqlite` | no | yes |
| Artifact manifests and bytes | artifact CAS | no | yes |
| Memory body and assertions | canonical UTF-8/LF Markdown | no | yes |
| Jobs, deliveries, acks, consumer cursors | `memory-ops.sqlite` | no | yes |
| FTS, KG, timeline | `memory-index.sqlite` | yes | no |
| Vectors and payload index | Qdrant local or explicit server mode | yes | no |

`reindex --full` may replace only derived stores. It must never clear jobs,
deliveries, acknowledgments, or consumer cursors.

## Principal and visibility enforcement

Resolve tokens in constant time to the principal schema. Request fields cannot
override author or acting subject. Enforce both scope and visibility on every
read surface: direct note read, snippet, search score, timeline, graph, inbox,
wakeup, and aggregate stats.

- `private`: author or `memory.admin` only.
- `shared`: any principal with `memory.read`.
- `direct:<subject>`: author, named recipient, or `memory.admin`.
- Shared/direct writes require both `memory.write` and `memory.share`.
- Supersedes requires same author or `memory.admin`.

The complete allow/deny oracle is
`tests/contracts/fixtures/security/access-matrix.json` and is executable.

## Durable state machines

For a note create, write and flush a temporary file, atomically rename it into
the memory tree, and durably enqueue the operation before returning `201`. A
startup scan repairs the rename-to-job crash window. Job, delivery, task-log,
and ack transitions use deterministic operation IDs and unique constraints.

Delivery ID is the canonical hash of `(noteId, recipient, reason)`. Task-log ID
is `tasklog:<taskId>:<terminalEventSeq>`. Ack commit is monotonic and retrying a
committed ack succeeds without moving backward. Scenario fixtures under
`tests/scenarios/fixtures/` declare each deterministic failpoint and expected
recovery; executable kill/restart tests provide the persistence evidence.

## Untrusted memory and safe defaults

All memory, task payloads, peer messages, and derived artifact text remain data.
Wakeup uses explicit item boundaries, provenance, and trust labels and is never
placed in the system role. If context delivery fails, do not acknowledge the
inbox page.

Task-log sedimentation is disabled unless an intent is allowlisted. The runtime stores
structured task identifiers/state/actors/timestamps only; raw task payload is
used solely for eligibility checks and is not written to the note. Private
memory is excluded from public/git backup by default. MCP
annotations and Skill prose are hints, never authorization.
