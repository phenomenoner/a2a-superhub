# A2A Superhub

> **Your agents collaborate. Then they forget everything.**
>
> A2A Superhub is a durable coordination hub for heterogeneous AI agents — with a
> shared **memory plane** (opt-in durable memory, offline sharing, hybrid
> retrieval, and a standards-based MCP sidecar) where collaboration history becomes knowledge
> any agent can query. Even the agents that were offline when it happened.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-3776ab.svg)](pyproject.toml)
[![Runtime deps](https://img.shields.io/badge/runtime%20deps-zero-brightgreen.svg)](pyproject.toml)
[![Memory plane](https://img.shields.io/badge/memory%20plane-MCP%20integrated-16a34a.svg)](docs/DESIGN.md)

**[Product site](https://phenomenoner.github.io/a2a-superhub/)** ·
**[Shared memory design and implemented surfaces](docs/DESIGN.md)** ·
[Agent user guide](docs/AGENT_USER_GUIDE.md) ·
[Local agent operations](docs/LOCAL_AGENT_OPERATIONS.md) · [API](docs/API.md) ·
[Operations](docs/OPERATIONS.md) · [Adapters](docs/ADAPTERS.md) · [Security](docs/SECURITY.md)

---

## The problem

Modern agent stacks are heterogeneous by default: one team runs an A2A-capable
service, another exposes MCP tools, another has an ACP editor adapter, another
only has a CLI. Making them work together hits three walls:

1. **N×N glue.** Every agent pair invents its own integration, again.
2. **Session amnesia.** Work products survive; the *context* — who decided what,
   why, and what was learned — dies with the session.
3. **Absent peers stay ignorant.** What Agent A learns about Agent B never
   reaches B, unless a human plays messenger.

Superhub attacks all three with one small, local-first hub.

## Two planes, one hub

| Plane | Status | What it gives you |
|---|---|---|
| **Coordination plane** | v1 source surface | Durable task lifecycle, progress events, content-addressed artifacts, Agent Card registry, idempotency, bearer auth, and rate limits. Dependency-free Python + SQLite. |
| **Memory plane** | 0.3.0 source contract (opt-in) | `memory.v2` logical delivery, exact inbox ACK, bounded wakeup preview, safe typed errors, authorized lifecycle facts, Markdown truth, FTS5 fallback, optional hybrid retrieval, and an MCP stdio sidecar. This is a repository-source description, not a package-release, live-deployment, SLA, or supported-workload claim. |

Agents remain peers, not children of a central framework. The hub owns
cross-agent semantics; adapters own local runtime integration.

## The moment that sells it

> **Monday 09:12** — you tell Agent A: *"B's gateway keeps dropping tokens after
> restarts."* Agent A writes a memory note tagged `about: [agent.beta]`.
>
> **Thursday 03:40** — Agent B wakes up, pulls its memory inbox, and can
> retrieve the original note with full provenance: who said it, when, and in
> which task.

```mermaid
sequenceDiagram
  participant U as User
  participant A as Agent A
  participant H as Superhub
  participant B as Agent B (offline)

  U->>A: "B's gateway keeps dropping tokens"
  A->>H: POST /v2/memory/notes {type: observation, about: [agent.beta], body: verbatim}
  H->>H: markdown note → SQLite FTS index → logical delivery for agent.beta
  Note over B: ...days later, B starts a session...
  B->>H: GET /v2/memory/wakeup?consumerId=desktop.startup
  H-->>B: bounded preview, no acknowledgeable cursor
  B->>H: GET /v2/memory/inbox?consumerId=desktop.startup
  H-->>B: one logical item + reasons + exact page cursor
  B->>H: POST /v2/memory/inbox/ack after the page is accepted
  Note over B: B now has provenance-rich data and opt-in hybrid search.
```

Memory sharing becomes **asynchronous message passing**: writing is delivery,
querying is catching up. No agent has to be online at the same time as any other.

## Current repository surfaces

The source tree currently includes the following surfaces. Their presence does
not by itself claim a published release, production deployment, or operational
readiness; the coordination core remains dependency-free:

- Standalone state root with SQLite task and event storage.
- Task create / get / list / cancel / event operations with idempotency keys.
- Content-addressed artifact store with SHA-256 verification, raw binary and
  restart-safe resumable uploads, private/shared/direct visibility, and bounded
  PDF text plus optional Tesseract OCR derivation.
- A2A 1.0 `Part` oneof mapping for `text`, `raw`, `url`, and `data`; the full
  protocol binding remains distinct from the legacy JSON-RPC facade.
- Agent Card registration and listing.
- Minimal JSON-RPC A2A facade: `message/send`, `tasks/get`, `tasks/cancel`.
- Optional bearer-token auth and per-client rate limiting.
- CLI and HTTP server, Python standard library only.
- Optional MCP 2025-11-25 stdio sidecar with ten memory/task tools, authorized
  resources, subscription notifications, and polling fallback guidance.
- Payload-free admin diagnostics plus offline authoritative backup/clean restore,
  recoverable retention, and parity-gated Qdrant provider activation/rollback.

### Quickstart

```bash
python -m venv .venv
. .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .

a2a-superhub --state ./state init
a2a-superhub --state ./state serve --host 127.0.0.1 --port 8787
```

```bash
curl http://127.0.0.1:8787/healthz
curl -s http://127.0.0.1:8787/v1/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "fromAgent": "agent.alpha",
    "toAgent": "agent.beta",
    "intent": "agent.query",
    "idempotencyKey": "demo-001",
    "payload": {"summary": "Summarize the attached artifact"}
  }'
```

Full API reference in [docs/API.md](docs/API.md). Adapter contract in
[docs/ADAPTERS.md](docs/ADAPTERS.md). MCP setup and exact behavior are in
[docs/MCP_AGENT_INTEGRATION.md](docs/MCP_AGENT_INTEGRATION.md).
Artifact transport, derivation, trust labels, and rollback behavior are in
[docs/ARTIFACTS_AND_DERIVATION.md](docs/ARTIFACTS_AND_DERIVATION.md).
For an authenticated loopback hub with separate agent identities, a packaged
Skill, secret-safe MCP host configuration, and an authorized cross-agent smoke
flow, follow [Run a local hub for agent use](docs/LOCAL_AGENT_OPERATIONS.md).

### Connect an MCP client

Keep the HTTP hub running with memory enabled, then configure the client to
launch the stateless sidecar:

```bash
pip install -e ".[memory-core,mcp]"
export A2A_SUPERHUB_URL=http://127.0.0.1:8787
export A2A_SUPERHUB_TOKEN=replace-with-a-token-handle
a2a-superhub-mcp
```

On Windows PowerShell, set the variables with `$env:A2A_SUPERHUB_URL=...` and
`$env:A2A_SUPERHUB_TOKEN=...`. The token belongs in the environment, not in the
MCP command line. Each sidecar holds no hub state and can be restarted or run
alongside other clients.

## Memory plane: 0.3.0 hardening contract (opt-in)

The full design is public — **[docs/DESIGN.md](docs/DESIGN.md)**. Durable memory is available
only with `pip install -e ".[memory-core]"` and `serve --enable-memory`; it is
off by default and preserves the coordination-only runtime. Delivery, task-log,
and watcher repair remain separately gated. The foundation has repository-level
end-to-end and restart/replay coverage; it is not a release, SLA, soak, or
operational-readiness claim. The short version:

### Memory v2 behavior and compatibility

Version 0.3.0 makes `memory.v2` the current HTTP, client, and MCP memory-sharing
surface:

- **One logical delivery per note and recipient.** If a note matches `about`,
  `direct`, and `handoff`, v2 returns one inbox item with a bounded, deduplicated
  `reasons` array. Delivery identity no longer depends on a single reason.
- **Wakeup is preview-only.** `/v2/memory/wakeup` is byte-bounded, reports
  section-level `hasMore`, and never returns an acknowledgeable cursor.
- **Only an exact inbox page can be acknowledged.** Fetch
  `/v2/memory/inbox`, deliver that page to the intended consumer, then send its
  cursor to `/v2/memory/inbox/ack`. Preview assembly, transport failure, and
  process restart do not change durable unread state.
- **Typed failures remain safe across transports.** HTTP, `HubClient`, and MCP
  preserve code, safe message, retryability, bounded validation details, and a
  trace ID. Submitted values, credentials, cursors, and filesystem locations
  are not validation details.
- **Lifecycle is a set of authorized facts.** Stored, indexed, queued,
  acknowledged, and linked-reference facts are independently projected. They
  never claim that a receiver understood, accepted, or executed a note.

The v1 endpoints continue to write and serve their per-reason compatibility
projection throughout 0.3.x; 0.4.0 is the earliest version that may remove it.
Stored Markdown remains `a2a-superhub.memory.note.v1`, so existing notes are not
rewritten. New v2 writes require typed relation targets such as `agent:`,
`note:`, `project:`, `task:`, `event:`, or `artifact:`. See
[Memory sharing v2 compatibility and state migration](docs/MEMORY_V2_COMPATIBILITY.md)
for cursor migration and rollback boundaries.

Operational limits are explicit: HTTP JSON bodies are at most 1 MiB; note
bodies are at most 262,144 UTF-8 bytes; titles are at most 256 code points;
search and inbox pages are capped at 100 items; and the complete wakeup envelope
is at most 65,536 UTF-8 bytes. Artifact uploads default to a 64 MiB cap, and
inline raw artifact parts are capped at 262,144 bytes. Memory, delivery,
task-log, watcher side effects, hybrid retrieval, and artifact derivation remain
opt-in as separately declared.

**Three ingredients, deliberately boring:**

1. **Markdown is the database.** Every memory is a plain `.md` file with YAML
   frontmatter and `[[wikilinks]]` — human-readable, git-versionable,
   Obsidian-compatible. Agents and humans edit the same files.
2. **A memory layer, not a summarizer.** Verbatim in, intelligence out: notes are
   stored word-for-word (no LLM extraction at write time). Structure comes from
   explicit frontmatter and links. Temporal validity is an explicit
   `supersedes:` chain, not model guesswork.
3. **Opt-in hybrid retrieval with FTS5 fallback.** Qdrant dense+sparse candidates
   are authorization-filtered in every prefetch and authorized again against
   Markdown. The default core remains dependency-free and keyword-only.

**On top of that:**

- **Knowledge graph + timeline** — entities (agents, humans, projects, topics,
  tasks, artifacts) and typed, timestamped edges in SQLite. Interaction context
  ("who said what about whom, when, in which task") is a query, not an inference.
- **Wake-up packs** — one preview call returns bounded profile, inbox, recent,
  and active-task context without an ACK cursor. When anything is omitted, the
  response points the caller to the exact inbox read path.
- **Task-log sedimentation** — when explicitly enabled for an allowlisted intent,
  terminal hub tasks can become structured memory notes without raw payloads.
- **MCP sidecar + reference adapter + operator Skill** — ten stable tools and
  two `memory://` resources reuse the HTTP authorization boundary; a removable client adapter negotiates
  identity/capabilities, inserts only delimited untrusted data, and acknowledges
  only an exact inbox page after delivery. The packaged Skill provides local bootstrap,
  identity-bound MCP launch, doctor, smoke, and install workflows.
- **Searchable artifact text** — when explicitly enabled, bounded PDF extraction
  and image OCR create a clearly labeled untrusted Markdown note. Every read and
  search result is re-authorized against the current source artifact manifest;
  cleanup removes the derived note/index without deleting the checksum-authoritative source.
- **Burn-the-index guarantee** — the current FTS/KG SQLite index is derived.
  Delete it and rebuild the same visible note/edge set from Markdown. Delivery,
  ack, job, task, artifact, and auth state are separate authoritative backups.

## How it compares

| | A2A task coordination | Durable shared memory | Knowledge graph + timeline | Offline inbox catch-up | Local-first, no API keys |
|---|:-:|:-:|:-:|:-:|:-:|
| **A2A Superhub (opt-in shared memory with MCP)** | ✅ | ✅ | ✅ | ✅ | ✅ |
| [mem0](https://github.com/mem0ai/mem0) — app↔user memory | — | ✅ | partial | — | partial |
| [memX](https://github.com/MehulG/memX) — realtime shared state | — | — (ephemeral KV) | — | — | ✅ |
| A2A registries — agent directories | discovery only | — | — | — | varies |
| [basic-memory](https://github.com/basicmachines-co/basic-memory) — human↔AI notes | — | ✅ | ✅ | — | ✅ |

Memory frameworks remember *users*. State layers share *the present*. Superhub
gives a fleet of peer agents a durable, queryable, **shared past**.

The source surface has repository end-to-end, restart/replay, official MCP
SDK, artifact transport/derivation, and cross-transport evidence. It does not
mean complete A2A 1.0 parity, production deployment, operational soak, audio
transcription, or image captioning.

## Roadmap

- **Contract and security baseline — 🧱 Foundation:** executable identity,
  note, API, protocol, package, and Skill contracts.
- **Durable memory and offline sharing — 🧱 Foundation (opt-in):** durable
  Markdown note v1, logical-delivery memory v2, exact inbox ACK, preview-only
  wakeup, separated operational/derived stores, FTS, a reference adapter, and
  an operator Skill. Per-reason v1 compatibility remains through 0.3.x.
- **Hybrid retrieval — 🧱 Foundation (opt-in):** Qdrant dense+sparse retrieval
  with authorization pushdown and keyword fallback.
- **MCP agent integration — implemented in source (opt-in):** ten stable memory/task
  tools, authorized resources, negotiated subscriptions with poll fallback,
  cross-transport scenarios, and Skill/product drift CI.
- **A2A 1.0 runtime binding — 📐 Design RFC:** a standards-compliant binding
  remains separate from the legacy JSON-RPC coordination facade.
- **Artifact text derivation — implemented in source (opt-in):** bounded PDF extraction,
  optional Tesseract OCR, source backlinks, current-ACL enforcement, durable
  idempotent jobs, explicit retry/cancel, and derived-note-only purge.
- **Additional media providers — 🗺 Planned:** image captioning and audio/video
  transcription remain provider work, not implied by OCR support.
- **Operational controls — 🧪 Validation in progress:** authoritative backup/clean
  restore, recoverable retention, payload-free diagnostics, and Qdrant migration
  are implemented. General garbage collection remains absent; the supported
  workload claim waits for the published package/rollback and 24-hour soak evidence.
- **Hub federation — 🗺 Planned:** namespaced, explicitly trusted hub-to-hub
  memory exchange.
- **Coordination hardening — mixed:** A2A Part-model validation and chunked
  artifact upload are implemented; SSE streaming, the complete A2A 1.0 binding,
  and push notifications remain planned.

Details and acceptance criteria in the [RFC](docs/DESIGN.md).

## Status & contributing

This project is **contract-first**: the repository contains coordination plus
opt-in durable memory, offline sharing, hybrid retrieval, MCP agent integration,
and artifact text derivation with executable test coverage; the complete A2A
1.0, additional-media, operational, and federation surfaces remain incomplete.
Read the [RFC](docs/DESIGN.md), the
[contract and security decisions](docs/CONTRACT_AND_SECURITY_DECISIONS.md), and the machine schemas before
opening an issue that starts with *"this breaks when…"*.

## Security posture

Local-first. Bind to loopback by default, use bearer tokens across trust
boundaries, treat every peer message and artifact as untrusted input. Memory
adds visibility scopes (`shared` / `private` / `direct:<agent>`) and
provenance on every write. See [docs/SECURITY.md](docs/SECURITY.md).

## Development

```bash
python -m pip install -e ".[contracts,derive]"
python -m unittest discover -s tests -v
```

This is the canonical clean-development command and is exercised on Windows and
Linux with Python 3.11 and 3.12 in CI. The `contracts` extra contains test-only
official A2A/MCP parsers and JSON Schema validation; the coordination core still
has zero runtime dependencies. Python 3.13 is not in the supported matrix yet.

The packaging contract also defines `memory-core`, `search`, `mcp`, `derive`,
and the `memory` umbrella extra. `memory-core` enables durable memory only when
the server flag is also present. `search` installs the selected FastEmbed
multilingual MiniLM + BM25 Qdrant provider; use explicit local/server search flags.
`mcp` installs the stateless stdio sidecar; `derive` installs pinned `pypdf` and
Pillow dependencies. Image OCR additionally requires a separately installed
Tesseract executable. See [docs/PACKAGING.md](docs/PACKAGING.md).

## License

MIT
