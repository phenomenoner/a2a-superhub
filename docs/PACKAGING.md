# Optional dependency contract

Status: install contract for the opt-in memory foundation and packaged
operator Skill. Installing `memory-core` makes the implemented foundation
available but does not enable it; explicit runtime flags remain required.
The MCP, search, and bounded artifact-text runtimes are implemented but opt-in.
Image OCR additionally needs a separately installed Tesseract executable.
Runtime capability discovery remains authoritative.

| Install | Exact optional dependency seam | Capability status |
|---|---|---|
| `a2a-superhub` | none | Shipped v1 coordination; zero runtime dependencies |
| `a2a-superhub[memory-core]` | `PyYAML==6.0.3`, `watchdog==6.0.0` | Durable memory, offline sharing, watcher, adapter, and Skill support; off by default |
| `a2a-superhub[search]` | `qdrant-client[fastembed]==1.18.0` | FastEmbed + Qdrant local/server hybrid retrieval; off by default |
| `a2a-superhub[mcp]` | `mcp==1.28.1` | Stateless MCP 2025-11-25 stdio sidecar; ten tools, authorized resources, and subscriptions |
| `a2a-superhub[derive]` | `pypdf==6.6.1`, `Pillow==12.2.0` | Bounded local PDF extraction and image validation; Tesseract OCR is an external executable provider; off by default |
| `a2a-superhub[memory]` | union of all dependency families above | Durable memory, hybrid search, MCP, and artifact-text derivation are available behind independent runtime flags |

`derive` pins the pure-Python PDF and image decoding libraries used by the
reference providers. Tesseract remains separately installed because it is a
platform executable. Captioning and transcription providers still own their
future dependencies, licenses, egress policy, and scenario evidence.

`search` pins the selected Apache-2.0 multilingual MiniLM ONNX source at revision
`faf4aa4225822f3bc6376869cb1164e8e3feedd0` and Qdrant BM25 at revision
`e499a1f8d6bec960aab5533a0941bf914e70faf9`. Model files remain derived cache
data. Local/server choice is based on measured latency, build time, RSS, and
derived-index bytes rather than a fixed point-count threshold.

The machine authority is `schemas/package-extras-v1.json`. Contract tests compare
it with `pyproject.toml`, assert the umbrella union, assert zero unconditional
runtime requirements, and import each selected extra in a fresh environment.
CI creates a separate installation job for every extra and the umbrella.
