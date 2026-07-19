# Durable memory foundation evidence

Baseline commit: `af8170fcdb9ce8a3817b5f2121608240a34ec519`
Evidence date: 2026-07-19
Scope: **durable storage and indexing only** — this record does not claim the
offline inbox, wakeup, task-log, adapter, Skill, MCP, or hybrid-retrieval behavior

Independent verification accepted this foundation on 2026-07-19. Later evidence
records cover the sharing and adapter layers built on it.

## Implemented behavior map

| Capability | Runtime assertion |
|---|---|
| Task lifecycle ordering | Transition graph, immutable terminal states, SQLite-serialized concurrent sequence allocation, stable v1 backfill |
| Transactional terminal outbox | Terminal event and deterministic outbox record in one transaction; durable replay and idempotent ack |
| Principal and scope enforcement | Validated static principals, constant-time bearer comparison, loopback policy, scope enforcement, server-derived authorship |
| Contained atomic Markdown writes | Contained normalized paths and fsync + atomic replace Markdown writer |
| Safe parsing and quarantine | Safe YAML, UTF-8/LF roundtrip, validation, two-pass duplicate/partial/path-collision quarantine |
| Durable operational store | Versioned ops DB, scoped idempotency, durable/requeued jobs, forward/backup-rollback/forward evidence |
| Disposable derived index | Derived notes/relations/FTS/manifest, source/index hash+revision separation, generation rebuild and atomic swap |
| Filesystem convergence watcher | Injected-clock debounce queue, watchdog-compatible callback, rename/delete convergence, startup full scan and polling fallback |
| Note service, CLI, and HTTP | CLI/service and opt-in HTTP create/read/list/keyword-search with canonical limits/errors |
| Authoritative final authorization | Final authorization against current Markdown on direct reads and every search/list hydration |

## Fail-first and regression evidence

The first `Task lifecycle ordering` run against the pre-change store failed as expected:
one `KeyError` for absent event sequence and two failures because terminal
mutation and state regression were accepted. After the implementation, the
targeted event suite passes concurrent writers, contiguous ordering, stable
read order, migration, transition, terminal, and outbox assertions.

The durable-memory suites also cover principal-scoped idempotency, API/CLI attribution,
body and JSON size boundaries, stable error envelopes with no token/path leak,
source-ahead/index-stale reporting, stale shared-to-private zero disclosure,
duplicate fail-closed/recovery, and burn-the-index behavior without ops mutation.

## Process-kill and active-mutation scenarios

`tests/scenarios/test_memory_kill_restart.py` uses child processes, deterministic
callback barriers, and real process termination; it does not use sleeps.

- Kill after temp fsync/before replace: no committed note; retry reuses the
  reserved principal/operation/idempotency identity.
- Kill after replace/before job: startup scan creates the missing deterministic
  job and the note becomes indexed/readable exactly once.
- Kill after job/before response: retry returns the original note.
- Kill between terminal event insert and outbox insert: the transaction rolls
  back both. Kill after commit/before return: restart observes one terminal
  event and one deterministic outbox operation; ack retries create no rows.
- Kill during a new index generation: the old generation remains queryable;
  restart can rebuild and atomically swap.
- Kill between indexed note upsert and manifest revision advance: SQLite rolls
  the transaction back; startup requeues and replay advances the revision once.

`tests/scenarios/test_memory_watcher.py` performs active filesystem mutation for
duplicate introduction/removal, partial-edit bursts, rename, delete, and restart
full-scan convergence. Every duplicate path is quarantined and the colliding ID
is absent from direct read, search, and raw derived manifest until repaired.

## Verification record

The authoritative final command outputs are recorded after the last contract
fingerprint update. Local Windows evidence and actual Linux evidence are listed
separately so workflow matrix coverage is not confused with an executed run.

| Environment | Command | Verification level | Result |
|---|---|---:|---|
| Fresh Windows x64, Python 3.12.5 | install editable `.[contracts,memory-core]`; `python -m unittest discover -s tests -v` | integration-level/scenario/replay-level | 59 run: 58 pass, 1 selected-extra skip |
| Same fresh Windows environment | `A2A_TEST_EXTRA=memory-core`; packaging contract | integration-level | 4/4 pass |
| Actual Linux container, `python:3.12-slim` | read-only source mount copied into disposable container; install `.[contracts,memory-core]`; full suite | integration-level/scenario/replay-level | 59 run: 58 pass, 1 selected-extra skip |
| Windows host | durable-memory process-kill and active-filesystem scenarios (included above) | scenario/replay-level | 8/8 pass |
| Windows host | Skill quick validation; normalized fingerprint tests | integration-level | valid; 4/4 pass |
| Worktree | `git diff --check` | static | pass; line-ending conversion warnings only |

The first fresh-venv install invocation exceeded the command-capture timeout,
so that invocation is not counted as evidence. The resulting environment was
independently checked for `PyYAML 6.0.3`, `watchdog 6.0.0`, `jsonschema 4.26.0`,
`a2a-sdk 1.1.1`, and `mcp 1.28.1` before the successful fresh test runs above.

Linux is actual executed Docker evidence for Python 3.12, not merely workflow
coverage. `.github/workflows/ci.yml` additionally defines Ubuntu/Windows on
Python 3.11/3.12 and selected-extra jobs, but those unexecuted CI cells are not
claimed as current evidence.

The Linux run copied the current durable-memory worktree from a read-only host mount into
the disposable container; it did not test only baseline `HEAD`. Source baseline
was `af8170fcdb9ce8a3817b5f2121608240a34ec519` plus the documented working-tree
changes. Image provenance was
`python@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de`.
The exact command was:

```powershell
docker run --rm -v "${PWD}:/source:ro" -w /workspace python:3.12-slim sh -lc "set -e
cp -a /source/. /workspace/
python -m pip install -q -e '.[contracts,memory-core]'
python -m unittest discover -s tests -v"
```

It exited `0` with `Ran 59 tests`: 58 passed and one selected-extra test skipped
because `A2A_TEST_EXTRA` was intentionally unset. The separate Windows selected
package class run executed four tests (three packaging invariants plus the
selected `memory-core` import) and passed 4/4.

## Completeness verdict

| Boundary | Required | Reached | Open gap | Blocks this foundation claim |
|---|---:|---:|---|---|
| Event ordering and terminal outbox | scenario/replay-level | scenario/replay-level | None | No |
| Principal/auth/scope and final ACL | scenario/replay-level | scenario/replay-level | None | No |
| Markdown atomicity/parser/quarantine | scenario/replay-level | scenario/replay-level | None | No |
| Ops/idempotency/migration/restart | scenario/replay-level | scenario/replay-level | None | No |
| Derived index/revision/rebuild/watcher | scenario/replay-level | scenario/replay-level | None | No |
| CLI/service/HTTP create and read | scenario/replay-level | scenario/replay-level | None | No |
| Long-run load/soak and live remote deployment | live/soak-level | Not required for this foundation claim | Not performed | No |

The fail-first event test, process-kill matrix, active filesystem mutation,
negative authorization/size/schema cases, caller review, fresh cross-platform
suite, and agent-surface drift test satisfy the durable-memory project adapter.
Sharing, adapter, and Skill behavior is evidenced separately.

## Explicit boundary

`schemas/agent-surface-v1.json` reports `memoryFoundation: true`,
`memoryDefaultEnabled: false`, and full `memory: false`. Durable memory requires
the `memory-core` extra plus `serve --enable-memory`. No sharing, adapter, or
protocol-compatibility claim is included in this focused record.
