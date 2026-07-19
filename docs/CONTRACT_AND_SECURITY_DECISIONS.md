# Contract and security decisions

Status: **🧱 Foundation (opt-in)**. This register explains the product boundaries
encoded by the public schemas and executable tests. It uses behavior names so a
reader does not need the private planning backlog to understand the decisions.
Breaking these contracts requires an explicit API, schema, or capability version.

## Required foundations

| Boundary | Adopted public contract | Safe fallback |
|---|---|---|
| Protocol binding | A future standards-compliant surface targets normative A2A 1.0 protobuf. The existing JSON-RPC facade remains explicitly `legacy`; the two are never presented as equivalent. | Advertise only the legacy surface until the compliant binding exists. |
| Principal identity | A static local registry maps token identifiers to `subject`, `kind`, and scopes. Token material never enters responses, logs, receipts, or examples. | Permit only loopback `local.operator`, or require configured authentication before a remote bind. |
| Truth ownership | Markdown owns memory content; `memory-ops.sqlite` owns delivery, acknowledgement, and job state; SQLite FTS/graph views and Qdrant are disposable derived indexes. | Disable memory mutation if truth ownership cannot be preserved. |
| Operational durability | SQLite is the authoritative operational store. JSONL is export/debug output, never the sole cursor or acknowledgement record. | Do not expose inbox or acknowledgement operations without the durable store. |
| Task-log sedimentation | Automatic task-log notes are allowlist-only and structured by default. Raw excerpts require explicit policy. | Leave automatic sedimentation disabled. |
| Supersede authority | Only the original author or `memory.admin` may supersede an assertion. Other authors use `disputes` or `updates`. | Disable supersede mutation while retaining non-destructive relations. |
| Consumer cursor | Opaque cursors bind principal, consumer ID, and delivery sequence. Acknowledgement is monotonic and idempotent. | Advertise single-consumer support instead of implying multi-device semantics. |
| Wakeup safety | Wakeup returns a bounded `role=data`, `trust=untrusted-memory` envelope. Adapters and Skills must never elevate memory text to system instructions. | Disable wakeup and require explicit reads. |
| Embedding selection | A multilingual embedding model is accepted only after a fixed-corpus quality, license, CPU, memory, and latency evaluation. The selected model source, revision, license, dimension, and tokenizer/config bytes are pinned and verified at runtime. | Retain keyword-only FTS search. |
| Skill compatibility | The operator Skill negotiates the machine-readable API/schema/auth/capability surface before acting and ships with the same product version. | Stop at read-only discovery on mismatch. |
| Multi-consumer behavior | `consumerId` is part of the public contract. Any single-consumer fallback must be reported as a capability, never silently substituted. | Advertise the narrower behavior explicitly. |

## Supporting product choices

| Boundary | Adopted public contract | Safe fallback |
|---|---|---|
| Loopback no-token mode | No-token operation is loopback-only and maps to `local.operator` with fixed scopes. | Require configured auth on loopback too. |
| Write consistency | Atomic, durable Markdown plus a durable indexing job precedes `201`; derived views are asynchronous and report source/index revisions. | Return `202` until durability is established. |
| Tenant model | Room ACL and hostile multi-tenant isolation are outside the current single-operator trust domain. | Stop expansion and design an explicit tenant/ACL model before cross-team use. |
| Note mutation | Notes use immutable POST. Arbitrary body PATCH is absent. | Add only a separately versioned, `If-Match`-guarded metadata endpoint after review. |
| Local vector mode | Embedded Qdrant is a zero-operations pilot. The local-to-server decision uses measured latency, build time, RSS, and derived-index bytes rather than a magic point count. | Remain keyword-only. |
| Adapter scope | One removable reference adapter and one packaged operator Skill prove the vertical slice. A broader MCP/adapter matrix remains future work. | Keep the server usable without claiming cross-runtime integration. |
| Skill distribution | `skills/operate-a2a-superhub/` ships with source and wheel artifacts and is checked for contract drift. | Block distribution if the Skill and product surface diverge. |
| Private backup | Memory is never git-pushed automatically. Backup defaults to local/encrypted destinations; public targets require explicit opt-in. | Disable product-managed backup. |
| Federation | Hub-to-hub federation remains disabled until same-operator trust, namespaced identities, and operational evidence exist. | Keep hubs isolated. |

Ratification record: **APPROVED** on 2026-07-19. The executable contract is
the current schemas, tests, source, and agent-surface fingerprint; this prose is
the public explanation of those constraints.
