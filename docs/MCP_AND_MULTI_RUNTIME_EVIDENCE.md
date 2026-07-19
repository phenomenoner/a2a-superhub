# MCP and multi-runtime verification evidence

Date: 2026-07-20

This record covers the optional MCP `2025-11-25` stdio sidecar, the ten-tool
memory/task surface, authorized resources, subscription refresh, HTTP fallback,
adapter transport selection, product Skill synchronization, and a two-runtime
offline handoff. It is implementation and repository scenario evidence, not a
production deployment or long-running soak claim.

## Fail-first boundary

The first runtime contract run failed because the package had no
`a2a_superhub.mcp_server` module, no `a2a-superhub-mcp` entry point, and only a
six-tool planning contract with different names. The new contract tests were
kept and now require the runtime definitions and checked-in schema to match
exactly.

## Official protocol and stdio lifecycle

The tests use `mcp==1.28.1` types and its real stdio client/session lifecycle.
They verify:

- initialize negotiates protocol `2025-11-25`;
- the server advertises tools plus resources with `subscribe: true`;
- exactly ten public tools and two resource templates are listed;
- tool input/output schemas and annotations parse through official SDK models;
- note create/read and task create/status complete through a spawned sidecar;
- `memory://note/{id}` returns the authorized HTTP representation;
- closing the client cleanly terminates the stdio sidecar.

## Authorization and multi-runtime scenario

One scenario writes a direct handoff through the HTTP client, stops the hub,
restarts it on the same durable state, and consumes the handoff through a real
MCP client. The MCP wakeup resource retains its `role=data` and
`trust=untrusted-memory` boundary plus the original note and task provenance.

While the MCP client is subscribed to `memory://wakeup/agent.beta`, another HTTP
principal writes a new authorized observation. The sidecar emits a real
resource-updated notification; the refreshed resource contains the new note.
Inbox acknowledgment through MCP advances the issued cursor and a later fetch
is empty.

A separate read-only principal calls the annotated write tool and receives the
hub's `403` authorization denial. A follow-up authorized HTTP search confirms
the rejected note was not created. This demonstrates that MCP annotations and
the sidecar cannot bypass the HTTP authentication/scope boundary.

## Compatibility, fallback, and Skill synchronization

The adapter selection contract records three explicit outcomes:

| Negotiated surface | Selected path | Resource refresh | Mutation policy |
|---|---|---|---|
| Current MCP with subscriptions | MCP | subscribe | server-authorized |
| Current MCP without subscriptions | MCP | poll `resources/read` | server-authorized |
| Previous product exposing only a legacy Agent Card | HTTP | poll | read-only discovery |

The packaged Skill doctor performs read-only HTTP discovery and a real MCP
initialize/list-tools probe. It selects MCP only when negotiation succeeds,
never prints token material, and can be told to require HTTP or fail closed on
MCP. The trigger corpus accepts product-specific Superhub operations and rejects
generic memory, GitHub, A2A education, MCP education, vector-database, and
lookalike-product prompts. API, CLI, MCP schema, and Skill compatibility files
share one fingerprint; deliberate drift in each surface fails the contract test.

## Fresh verification results

- Dependency-complete Python 3.12 repository suite: **130 tests passed**, with
  **4 expected skips**, in **85.946 seconds**.
- Focused MCP, package, Skill, adapter, doctor, and multi-runtime pack:
  **25 tests passed** with **1 expected selected-extra skip**.
- Fresh isolated `.[mcp,memory-core]` install: both console entry points were
  present, `a2a-superhub-mcp --help` ran, the packaged Skill fingerprint
  validated, and all **6** selected-extra packaging tests passed.
- Built wheel inspection confirmed the MCP runtime module, packaged Skill, and
  MCP machine contract are present.
- Skill creator quick validation, Python bytecode compilation, JSON parsing,
  `git diff --check`, and public-hygiene scans passed.

The full suite intentionally skips the real Qdrant local/server search scenario
unless its explicit integration environment flag is set. Hybrid retrieval has a
separate evidence record; this MCP change re-ran all non-Qdrant regressions and
does not replace retrieval-provider evidence or operational soak.
