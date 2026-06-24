# Security Notes

A2A Superhub is designed for local-first agent coordination. It should start
small, fail closed, and make trust boundaries explicit.

## Defaults

- Bind to `127.0.0.1` unless you have a reason to expose the hub elsewhere.
- Use `--token` or `A2A_SUPERHUB_TOKEN` outside a single-process trust boundary.
- Treat every peer message, task payload, Agent Card, and artifact as untrusted.
- Prefer artifact references and checksums over large inline payloads.

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

The MVP server includes a small per-client fixed-window limiter. Production
adapters should also enforce per-peer, per-scope, and provider-specific limits.
