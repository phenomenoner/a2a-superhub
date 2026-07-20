# Security Notes

A2A Superhub is designed for local-first agent coordination. It should start
small, fail closed, and make trust boundaries explicit.

This file covers shipped coordination plus the opt-in memory and retrieval foundation.
The full memory plane identity, truth-store, visibility, cursor, and
untrusted-context contract is in [MEMORY_SECURITY.md](MEMORY_SECURITY.md).

## Defaults

- Bind to `127.0.0.1` unless you have a reason to expose the hub elsewhere.
- Use `--token` or `A2A_SUPERHUB_TOKEN` outside a single-process trust boundary.
- A non-loopback bind without a token or validated static principal registry is
  rejected. Static bearer comparison is constant-time and secrets are never
  included in errors or logs.
- Treat every peer message, task payload, Agent Card, and artifact as untrusted.
- Prefer artifact references and checksums over large inline payloads.
- Artifact owners come only from authentication. Shared/direct artifact writes
  require `artifact.share`, and list/read paths use the current manifest.

## Recommended adapter policy

Adapters should enforce local policy before executing any task:

- Prompt execution scope is not shell scope.
- Artifact read scope is not arbitrary filesystem scope.
- Platform-send scope is separate from agent reasoning scope.
- Live operations, destructive actions, purchases, trades, and external messages
  should require explicit higher-trust approval.

## Path handling

Do not pass peer-supplied paths directly to local runtimes. Store peer content in
Superhub's artifact CAS, verify checksums, then hand local adapters explicit,
policy-approved file references.

## Secrets

Do not put tokens, API keys, raw credentials, or private transcripts in task
payloads, Agent Cards, logs, or receipts. Use local secret stores and adapter-side
credential lookup.

## Rate limits

The MVP server includes a small per-IP-and-principal fixed-window limiter. Production
adapters should also enforce per-peer, per-scope, and provider-specific limits.

## Memory and retrieval enforcement

Memory writes derive author/source/time from the resolved principal. Shared and
direct visibility additionally require `memory.share`. Reads and search hydrate
the current Markdown note and repeat authorization after candidate selection,
so a stale shared-to-private index row cannot disclose content. Duplicate IDs,
partial YAML, unsafe paths, and normalized path collisions quarantine fail
closed. Note content remains untrusted data. Wakeup returns a bounded
`role=data`, `trust=untrusted-memory` envelope; the removable reference adapter
delivers that envelope only as delimited data and acknowledges only after the
runtime callback succeeds. Session-end handoff requires explicit authority.

Hybrid retrieval applies the visibility/author filter independently to every
dense and sparse prefetch and again to the fused query. It then compares the
payload content hash with current authoritative Markdown and repeats policy
authorization before returning a note. A stale row yields no result, score, or
snippet. Qdrant is derived and burnable; it never owns ops, delivery, or
acknowledgement truth.

## Artifact derivation enforcement

Derivers are default-off plugins. Raw and resumable upload apply byte limits,
whole-file checksums, atomic admission, duplicate-chunk conflict detection, and
partial cleanup. PDF/image providers apply compressed-byte, page/pixel, output,
and timeout limits. Encrypted/malformed inputs fail closed.

Derived Markdown begins with an explicit untrusted-data boundary and retains the
source ID, authoritative checksum, and provider version. Current source artifact
authorization is repeated for every derived-note read and search result, so a
stale index cannot preserve broader visibility. Admin purge removes only the
derived note/index; the source CAS has no corresponding delete route.
