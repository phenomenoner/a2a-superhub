# Offline sharing and context evidence

Status: **offline inbox, authorized views, safe wakeup, task-log sedimentation,
and receipt-chain behavior independently verified on 2026-07-19**.

Evidence date: 2026-07-19 (Asia/Taipei)

## Scope and verification altitude

This capability set changes durable operational state, cursor/ack semantics, authorization
views, HTTP/CLI contracts, and agent-facing Skill guidance. The required
altitude is therefore end-to-end restart/replay evidence, not unit-only
self-attestation. Evidence includes real subprocess kills, an actual HTTP server
restart, closed-schema validation of non-empty live responses, a fresh Windows
environment, and a pinned Linux container. No long-running soak or production-readiness claim
is made.

Adapter injection, MCP, A2A 1.0, and hybrid/vector retrieval are outside this
focused record; the current adapter and retrieval behavior is evidenced in
their own documents.

## Implemented behavior map

| Capability | Implemented behavior | Primary executable evidence |
|---|---|---|
| Deterministic delivery generation | Stable delivery ID from note/recipient/reason; about/direct/handoff; retry/watch/reindex dedupe | `test_offline_inbox.py::test_delivery_identity_is_stable_across_retry_watch_and_reindex`; delivery process-kill test |
| Durable multi-consumer inbox | Durable per-principal/consumer cursor ledger; fetch-no-ack; restart/reindex/secret rotation; stale/forged/unissued rejection; revoked poison advance | all `test_offline_inbox.py`; `test_inbox_kill_restart.py::test_fetch_and_ack_commit_response_kills_preserve_delivery_semantics`; live HTTP restart test |
| Authorized search, timeline, and graph | Real SQLite FTS5 with sanitized syntax/fallback, candidate auth before limit and final authoritative auth; newest-first timeline filters; 1-2 hop graph target hydration | `test_memory_views.py` FTS/timeline/graph tests, including private crowd-out and shared-to-private target zero-leak |
| Supersede and dispute semantics | Same-author/admin supersede; unauthorized rejection; caller-visible authoritative superseder set | `test_memory_views.py` supersede/history and private-successor tests |
| Bounded safe wakeup | Fixed profile/inbox/recent/active-task sections; global newest selection; whole-item UTF-8 budget; provenance/trust/delimiters; no ack | all `test_wakeup_context.py`, including injection, mixed auth, dead-lettered exclusion, and more-than-100-note recency |
| Allowlisted task-log sedimentation | Feature/intent default deny; completed/failed/canceled exactly once; structured fields only; raw payload never stored | all `test_tasklog_sedimentation.py`; task-log subprocess kill test |
| Sanitized operational statistics | Admin-only counts/revisions/degraded state; active versus historical quarantine resolution; no content | stats/receipt view test; watcher duplicate-repair stats assertions |
| Trace and receipt chain | Durable trace identity; replay reconciliation; atomic delivery+receipt and ack+receipt transactions; sanitized write-index-delivery-ack chain | stats/receipt view test; live HTTP receipt test; delivery/ack subprocess kill tests |

## Required scenario map

| Scenario family | Evidence |
|---|---|
| Authorization and stale-visibility scenarios | stale visibility and revoked inbox tests; FTS auth-before-limit; graph target hydration and private successor regressions; sanitized error/receipt tests |
| Markdown durability and recovery scenarios | missing-ID flag and kill windows; partial/duplicate/casefold watcher tests; delivery identity across rename/reindex; full rebuild; supersede authority |
| Offline inbox and acknowledgement scenarios | live offline HTTP restart; fetch/ack process kills; stable delivery; two consumers; stale/forged/unissued and rotated cursors; revoked poison |
| Task-log exactly-once scenarios | terminal variants; before-write/after-write-before-ack kills; disabled/oversize/secret suppression; terminal immutability |
| Wakeup injection-containment scenarios | injection fixture; UTF-8/item boundary; four-section mixed private/authorized fixture and global recency |
| Skill validation, compatibility, and trigger scenarios | compatibility fingerprint, old/absent capability fallback, trigger positive/negative/ambiguous corpus |

The offline adapter handoff is intentionally not claimed here; see
`ADAPTER_AND_SKILL_EVIDENCE.md`.

## Crash/restart and concurrency evidence

`tests/scenarios/test_inbox_kill_restart.py` uses child processes that block at a
named failpoint and are terminated by the parent process. Green windows:

- delivery commit to response: retry returns the same note/trace, stable logical
  deliveries, and a reconciled write/index/delivery receipt chain;
- fetch to response: restart still presents unread content;
- ack commit to response: restart does not redisplay, same-cursor retry is
  idempotent, and ack receipt exists;
- terminal outbox before note write and after note write before ack: exactly one
  task-log, raw payload absent;
- missing-ID before and after atomic replace: restart converges to one stable ID.

The compound idempotency-key barrier test runs eight concurrent writers and
proves one `inserted=true`, one note ID, and no errors. A separate two-writer
different-hash race proves one success and one conflict. Same-key thread
ownership uses a weak, compound-key lock registry; SQLite uniqueness remains
the cross-process authority and losers re-read/reconcile or fail closed for a
safe retry. A 100-key cleanup assertion proves the registry retains no locks.

## Live API and contract evidence

`tests/contracts/test_live_memory_http_contract.py` starts the real HTTP server
and validates non-empty create, search, inbox, ack, wakeup, and structured error
responses against `schemas/memory-api-v1.schema.json`. This prevents empty-array
fixtures from masking item-shape drift. The offline HTTP scenario additionally
proves restart catch-up, provenance, private poison omission, forged cursor
rejection, ack/no-redisplay, and sanitized write/index/delivery/ack receipts.

The agent surface and product Skill declare granular `memoryFoundation`,
`memorySharing`, `timelineGraph`, `safeWakeup`, `taskLog`, and watcher flags.
Delivery, task-log, and watcher side effects default off; `memoryFull` remains
false. The Skill compatibility fingerprint is executable and current.

## Fresh final verification

### Windows

Environment: fresh Python 3.12 virtual environment, editable product install
with `memory-core` and `contracts` extras.

```text
python -m unittest discover -s tests -v
Ran 90 tests in 189.776s
OK (skipped=1)
```

Result: **89 passed, 1 expected skip**. The only skip is the separately selected
fresh-extra matrix (`A2A_TEST_EXTRA` is intentionally unset); memory-core and
contracts dependencies were installed and all official A2A/MCP/JSON contract
parsers ran.

### Linux

Image provenance:
`python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de`.
The read-only workspace was copied inside the disposable container before build
so packaging could generate temporary egg metadata without mutating the host.

```text
PYTHONPATH=/work/src python -m unittest discover -s tests -v
Ran 90 tests in 73.577s
OK (skipped=1)
```

Result: **89 passed, 1 expected selected-extra skip**, matching Windows.

## Boundaries of this focused record

- The reference adapter and automatic context injection are covered by the
  separate adapter-and-Skill evidence record.
- This focused record predates MCP delivery. MCP now has separate
  cross-transport evidence; legacy JSON-RPC and the still-unimplemented A2A 1.0
  binding remain identified separately.
- This record exercises SQLite FTS5 plus deterministic LIKE fallback; hybrid
  Qdrant retrieval is covered by the separate retrieval evidence record.
- No long-running soak, production deployment, or cutover is claimed.
