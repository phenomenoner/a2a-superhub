# Reference adapter and operator Skill evidence

Status: **reference adapter, packaged operator Skill, offline handoff, and
artifact installation independently verified on 2026-07-19**.
This focused record is not a release, deployment, soak, or production-readiness claim.

Evidence date: 2026-07-19 (Asia/Taipei)

Baseline commit: `b93bef11b0f13be9d63b8f46dffc42e00b98de8e`
Initial implementation rebaseline: `af8170fcdb9ce8a3817b5f2121608240a34ec519`

The baseline advanced during implementation only through the external commit
`b93bef11`, which is an ancestor of the final working tree and adds `CLAUDE.md`
plus the public docs and website update guide. It has no implementation overlap
with this capability set.

## Scope and verification altitude

The reference adapter crosses the agent/runtime trust boundary, installs an agent-facing Skill,
changes durable inbox acknowledgement behavior, and packages runnable code.
The required altitude is therefore end-to-end restart/replay evidence plus artifact
installation evidence, not unit-only self-attestation. The final evidence uses
real HTTP servers, filesystem mutation, subprocess termination, fresh wheel and
sdist environments, pinned Linux containers, an installed-wheel agent workflow,
and real Markdown/SQLite corpus benchmarks.

The adapter surface is deliberately narrow: it is explicitly removable,
negotiates capabilities, injects only delimited untrusted data, and uses the
opt-in product Skill. MCP and an A2A 1.0 runtime binding remain future work.
Hybrid retrieval is implemented and evidenced separately; long-running soak and
production SLA claims remain open.

## Implemented behavior map

| Capability | Implemented behavior | Primary executable evidence |
|---|---|---|
| Reference adapter trust boundary | Removable reference adapter verifies negotiated capabilities and the server-authenticated principal; injects only delimited `role=data`, `trust=untrusted-memory`; acknowledges only after successful delivery; requires explicit authority for handoff | `tests/test_reference_adapter.py`; `tests/contracts/test_adapter_skill_contract.py`; installed-wheel forward trace |
| Operator Skill workflows | The product Skill provides read-only doctor, read/search/inbox/wakeup workflows and authority-gated writes; auth, connection, and version errors remain distinct; N-1 fallback occurs only for a missing current capability route | `tests/test_memory_client.py`; `tests/test_skill_scripts.py`; Skill trigger and compatibility tests in the full suite |
| Skill packaging and drift enforcement | Wheel and sdist embed the exact nine-file Skill payload and contract bundle; CLI path/validate/install/uninstall works with spaces; install is contained, owned, recoverable, and uninstall retains modified/unowned files | `tests/test_skill_installation.py`; Windows and Linux artifact matrices; Linux symlink containment cases |
| Offline handoff end to end | A writes a direct observation linked to a real task/event/artifact; offline B cold-starts after restart, receives untrusted data, verifies provenance, acknowledges, and sees no redisplay | `tests/scenarios/test_adapter_e2e.py`; `docs/evidence/adapter-agent-forward.json` |
| Cross-feature regression suite | Existing durable-memory and sharing kill/restart, stale visibility, two-consumer, cursor, duplicate, task-log, and prompt-injection packs remain green; production watcher handles atomic moves and duplicate IDs fail closed | focused 121-test four-environment matrix; `tests/scenarios/test_watcher_security.py`; prior durable-memory and sharing evidence records |
| Filesystem and SQLite performance baseline | Real filesystem/SQLite baseline records write, rebuild, FTS, delivery startup, and wakeup at 1/1,000/10,000 notes on Windows and Linux without an SLA claim | `benchmarks/memory_foundation_baseline.py`; `docs/PERFORMANCE_BASELINE.md`; machine-readable JSON results |

## Required scenario map

| Scenario ID | Executed evidence |
|---|---|
| Offline adapter handoff scenario | Adapter rejects system-role delivery, marks injected content as untrusted data, preserves unread state on delivery failure, and acks only after callback success. The installed-wheel trace independently records `role=data` and `trust=untrusted-memory`. |
| Skill compatibility fallback scenario | Missing current capability route falls back to read-only N-1 discovery; wrong-token 401 remains an auth failure and never falls back. |
| Skill authority-gating scenario | Existing-target smoke requires explicit URL and write authority; unsupported capabilities and destructive/session-end operations fail before mutation without explicit authority. |
| Skill trigger evaluation | Trigger corpus distinguishes positive product-operation prompts from generic memory explanation and ambiguous prompts. |

The full suite also reruns the authorization and stale-visibility, Markdown
durability and recovery, offline inbox and acknowledgement,
task-log exactly-once, and wakeup injection-containment packs. Passing those regressions is required because the
client, watcher, adapter, and package sit above their contracts.

## Installed-wheel agent workflow

`tests/scenarios/adapter_forward_actor.py` was orchestrated by this Codex agent task
using the fresh wheel environment from a directory outside the source tree.
Python isolated mode was enabled and `PYTHONPATH` was absent, so imports and the
canonical Skill came from the installed artifact.

The run completed in 1.484 seconds, below the 30-minute exit threshold. It:

1. ran the packaged read-only doctor;
2. created a real task, event, artifact, and direct observation;
3. cold-started B and delivered the observation as untrusted data;
4. verified the task/event/artifact provenance links;
5. acknowledged only after delivery and observed zero unread redisplays; and
6. wrote an explicitly authorized return handoff.

The observation included an instruction to create a marker inside the disposable
runtime state. The actor checked the filesystem after delivery and recorded
`promptInjectionCanaryAbsent=true`; `promptInjectionExecuted=false` is therefore
derived from an observable canary, not a literal self-attestation. The public,
token-free execution trace is `docs/evidence/adapter-agent-forward.json`.

## Packaging evidence

Focused adapter/Skill verification artifacts (superseded by any later rebuild):

| Artifact | SHA-256 |
|---|---|
| `a2a_superhub-0.1.0-py3-none-any.whl` | `46dab29316dbc21a9b5d9de6ee850d29efdfe9c54739584b302d632e6971c8e8` |
| `a2a_superhub-0.1.0.tar.gz` | `1f32b2ec998e5dff0ab13e5341fe14f1c78e726dd9021d8784d0bee25701b18a` |

Fresh Windows 3.12 and pinned Linux 3.12 matrices installed each artifact with
`memory-core`, discovered and validated the packaged Skill, installed its exact
nine-file allowlist into a target path containing spaces, ran the ephemeral
smoke workflow, and uninstalled it. Each artifact reported:

```text
validate=true
install=true, files=9
smoke ok=true, ephemeral=true, wakeupRole=data, unreadAfterAck=0
uninstall removed=true, removedFiles=9, retainedFiles=0
```

The final Linux artifact matrix exited 0 in 50.8 seconds. Linux additionally ran
both destination-link and late-file-symlink escape cases without a skip. The
installer refuses unresolved/relative roots, existing links/junctions, malformed
ownership manifests, unowned overwrite, and path escape. Force replacement
creates a recoverable backup. Uninstall preflights every owned path before any
removal and removes only unchanged owned files; it does not delete hub data,
tokens, or user state.

The Skill Creator structural validator returned `Skill is valid!`. Regenerated
`agents/openai.yaml` bytes match the packaged copy. The product validator also
recomputes the agent-contract fingerprint rather than trusting a static string:
`sha256:bfcfa9c36a436fd9c9b4f8b7ddda44b87e489544818c45711bb9ead92d7b39d4`.

## Fresh final verification

All four legs ran the current 121-test suite after the last production-code and
test change. Windows used fresh dependency-complete virtual environments;
Linux installed the rebuilt wheel with `memory-core,contracts`, mounted source
read-only, and did not set `PYTHONPATH`.

| Environment | Result | Effective passes | Skips |
|---|---|---:|---:|
| Windows 11, CPython 3.11.9 | `Ran 121 tests in 98.402s`, exit 0 | 118 | 3 |
| Windows 11, CPython 3.12.5 | `Ran 121 tests in 91.922s`, exit 0 | 118 | 3 |
| Linux, pinned CPython 3.11 | `Ran 121 tests in 64.356s`, exit 0 | 120 | 1 |
| Linux, pinned CPython 3.12 | `Ran 121 tests in 66.932s`, exit 0 | 120 | 1 |

The single cross-platform skip is the intentionally unselected extra sentinel.
Windows has two additional expected skips because the host lacks the privilege
to create test symlinks. Both tests execute and pass on Linux. Pinned images:

- `python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93`
- `python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de`

## Fail-first and regression evidence

The final implementation incorporates failures found at the appropriate
altitude rather than treating an earlier green unit set as completion:

- a production watcher initially missed atomic temp-to-Markdown moves;
- the polling fallback could die when a file disappeared between glob and stat;
- duplicate external IDs needed immediate fail-closed catalog removal;
- a direct-create/runtime-watcher race could retain the watcher's generated
  delivery trace instead of the caller's explicit trace;
- Python 3.11 rejected a nested f-string/backslash expression accepted by 3.12;
- uninstall originally needed whole-operation preflight to prevent partial
  removal before a later escape was discovered; and
- the first final Linux benchmark setup omitted `memory-core` dependencies and
  failed before measuring a size.

Each became a regression test or an explicitly excluded setup attempt. Earlier
Windows benchmark attempts that timed out, orphaned a child, overlapped another
run, or predated the final receipt-path change were discarded. Only the
uncontended final-code results in `docs/PERFORMANCE_BASELINE.md` are claimed.

## Exit checklist and open boundaries

- Fresh installed agent workflow completes doctor → wakeup → inbox → handoff in
  under 30 minutes: **evidenced**.
- Wakeup is untrusted data, never a system instruction: **evidenced by contract,
  tests, delivered block, and absent execution canary**.
- Provenance reaches real task/event/artifact records: **evidenced**.
- Skill/API contract fingerprint is current and executable: **evidenced**.
- The durable-memory and sharing scenario/replay bundle, packaging matrices, and honest performance baseline exist:
  **evidenced and independently ratified**.

The complete memory-and-sharing foundation was independently verified: the
authorization and stale-visibility, Markdown durability and recovery, offline
inbox and acknowledgement, task-log exactly-once, and wakeup
injection-containment regression families are green; the adapter and Skill
completed real offline catch-up;
reindex preserved durable ops/ack state; and the feature-off coordination
runtime remained compatible. This is a scenario/replay-level conclusion only, not an
operational-readiness claim.

This focused record predates the hybrid-retrieval verification; see
`HYBRID_RETRIEVAL_EVIDENCE.md` for the selected embedding model and real local
and server Qdrant results. No long-running soak, production deployment, live
cutover, or production-readiness evidence is claimed.
