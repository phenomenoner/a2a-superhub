# Optional dependency contract

Status: install contract for the opt-in memory foundation and packaged
operator Skill. Installing `memory-core` makes the implemented foundation
available but does not enable it; explicit runtime flags remain required.
MCP and deriver provider seams do not imply implemented runtimes. Search is an
implemented but opt-in derived runtime.
Runtime capability discovery remains authoritative.

| Install | Exact optional dependency seam | Capability status |
|---|---|---|
| `a2a-superhub` | none | Shipped v1 coordination; zero runtime dependencies |
| `a2a-superhub[memory-core]` | `PyYAML==6.0.3`, `watchdog==6.0.0` | Durable memory, offline sharing, watcher, adapter, and Skill support; off by default |
| `a2a-superhub[search]` | `qdrant-client[fastembed]==1.18.0` | FastEmbed + Qdrant local/server hybrid retrieval; off by default |
| `a2a-superhub[mcp]` | `mcp==1.28.1` | Official SDK compatibility seam only; runtime not implemented |
| `a2a-superhub[derive]` | no shared dependency | Provider-owned multimodal dependency contract; runtime not implemented |
| `a2a-superhub[memory]` | union of all dependency families above | Memory and hybrid search are available opt-in; MCP and derivation runtimes remain absent |

`derive` is intentionally dependency-free, not a claim that a built-in deriver
exists. PDF, OCR, image, and transcription providers have different security,
license, and platform costs. Each future provider must own its dependency extra
and scenario evidence; the current contract does not choose one merely to make
this list nonempty.

`search` pins the selected Apache-2.0 multilingual MiniLM ONNX source at revision
`faf4aa4225822f3bc6376869cb1164e8e3feedd0` and Qdrant BM25 at revision
`e499a1f8d6bec960aab5533a0941bf914e70faf9`. Model files remain derived cache
data. Local/server choice is based on measured latency, build time, RSS, and
derived-index bytes rather than a fixed point-count threshold.

The machine authority is `schemas/package-extras-v1.json`. Contract tests compare
it with `pyproject.toml`, assert the umbrella union, assert zero unconditional
runtime requirements, and import each selected extra in a fresh environment.
CI creates a separate installation job for every extra and the umbrella.
