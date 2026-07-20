# Contract and security evidence

Baseline commit: `af8170fcdb9ce8a3817b5f2121608240a34ec519`
Evidence date: 2026-07-19
Evidence scope: **versioned identity, authorization, truth ownership, protocol,
package, and Skill contracts accepted as the implementation baseline**

## Historical fail-first evidence

These are captured pre-change/baseline results, not claims about the current
worktree:

- Before the contract artifacts existed, the canonical direct test command exited `1`
  with three import errors because the src-layout package was not installed.
- The first contract/security bundle test exited `1` and enumerated all eleven absent required
  artifacts.
- The packaging contract run against `git show HEAD:pyproject.toml` exited `1` with
  `PRE_CHANGE_MISSING_EXTRAS=derive,mcp,memory,memory-core,search`.

Current outputs are recorded separately below.

## Current fresh outputs

| Platform | Python | Command | Verification level | Result |
|---|---|---|---:|---|
| Windows x64 | 3.11.9 | fresh venv; `python -m pip install -e ".[contracts]"`; `python -m unittest discover -s tests -v` | integration-level | 26/26 pass |
| Windows x64 | 3.12.5 | same | integration-level | 26/26 pass |
| Linux container | 3.11.15 | `python:3.11-slim`; same install/test | integration-level | 26/26 pass |
| Linux container | 3.12.13 | `python:3.12-slim`; same install/test | integration-level | 26/26 pass |
| Windows host | current | `skill-creator/scripts/quick_validate.py skills/operate-a2a-superhub` | integration-level static | pass |

After packaging contract was added, the current Windows Python 3.11 and 3.12 full suites
each ran 30 tests: 29 passed and the environment-selected extra import test was
intentionally skipped. That same import test then passed in each of five separate
fresh Python 3.12 venvs:

| Selected extra | Fresh command | Result |
|---|---|---|
| `memory-core` | install `.[memory-core]`; set `A2A_TEST_EXTRA`; run packaging contract | 4/4 pass |
| `search` | initial no-FastEmbed env; see supersession record below | **SUPERSEDED — not current evidence** |
| `mcp` | install `.[mcp]`; same contract | 4/4 pass |
| `derive` | install `.[derive]`; same contract | 4/4 pass |
| `memory` | initial no-FastEmbed env; see supersession record below | **SUPERSEDED — not current evidence** |

The extra matrix is also encoded as five independent CI jobs. `derive` installs
pinned PDF/image decoding dependencies and validates the optional reference
providers; image OCR still reports unavailable unless a Tesseract executable is present.

### FastEmbed correction and supersession record

The initial environments below were created before scope review, when `search`
temporarily installed plain `qdrant-client==1.18.0`:

- a superseded fresh `search` virtual environment
- a superseded fresh `memory` virtual environment

They are **SUPERSEDED and must not be used as current packaging contract evidence**. In
particular, running the corrected packaging contract against the stale `search`
environment correctly fails because `fastembed` is absent.

The authoritative corrected fresh environments are:

| Extra | Exact fresh environment | Executed install/test/version commands | Current result |
|---|---|---|---|
| `search` | fresh isolated virtual environment | `py -3.12 -m venv <env>`; `<env>\Scripts\python.exe -m pip install ".[search]"`; set `A2A_TEST_EXTRA=search`; `<env>\Scripts\python.exe -m unittest tests.contracts.test_packaging_contract -v`; query installed package versions | 4/4 pass; `qdrant-client=1.18.0`; `fastembed=0.8.0` |
| `memory` | fresh isolated virtual environment | `py -3.12 -m venv <env>`; `<env>\Scripts\python.exe -m pip install ".[memory]"`; set `A2A_TEST_EXTRA=memory`; `<env>\Scripts\python.exe -m unittest tests.contracts.test_packaging_contract -v`; query installed package versions | 4/4 pass; `qdrant-client=1.18.0`; `fastembed=0.8.0` |

Both corrected tests import `qdrant_client` and `fastembed`. No embedding model
ID, revision, or model artifact was selected or downloaded.

The 26 tests include 2 official A2A protobuf-parser assertions, 2 official MCP
SDK assertions, 6 JSON/schema/path/evidence assertions, 3 full security-matrix
assertions, 4 Skill/trigger/fingerprint assertions, 1 deterministic scenario
skeleton assertion, 2 bundle/decision assertions, and 6 shipped-v1 regressions.

Live package-index evidence on 2026-07-19 reported `a2a-sdk==1.1.1`,
`mcp==1.28.1`, and `jsonschema==4.26.0`; those exact test-only versions are
pinned in the `contracts` extra. A2A proof parses fixtures into the official
normative-proto-derived SDK types; no local JSON Schema is presented as A2A
conformance. MCP proof starts at initialize negotiation and advertised
capabilities for protocol `2025-11-25`.

## Machine evidence map

| Gate | Evidence |
|---|---|
| Decisions/truth/security | `CONTRACT_AND_SECURITY_DECISIONS.md`, `MEMORY_SECURITY.md`, security matrix tests |
| Schema/API fixtures | `schemas/*.json`, memory/principal/API fixtures, JSON Schema tests |
| A2A 1.0 | `A2A_COMPATIBILITY.md`, official SDK-parsed Agent Card and SendMessage/Part fixtures |
| MCP | pinned initialize/capability/tool/resource contract parsed by official SDK |
| Crash/idempotency/cursor/stale auth | five deterministic scenario fixtures and runner |
| Skill | skill-creator layout, generated `openai.yaml`, trigger corpus, normalized fingerprint drift test |
| Optional packages | `package-extras-v1.json`, pyproject/umbrella equality, zero-core-dependency assertion, five fresh install/import tests |
| Supported baseline | Windows/Linux and Python 3.11/3.12 fresh matrix above |

## Ratification and remaining gaps

The product owner approved the protocol binding, principal identity, truth
ownership, operational durability, task-log policy, supersede authority,
consumer cursor, wakeup safety, embedding selection, Skill compatibility, and
multi-consumer behavior decisions on 2026-07-19. The executable bundle test
asserts that these named public decisions and their safe fallbacks remain present.

1. No canonical HTTPS shared-memory extension endpoint is advertised. Local
   URI-to-version mapping is tested, but live fetchability and full A2A 1.0
   interoperability remain future protocol work.
2. This focused record proves executable contracts at integration level. The
   separate durable-memory, sharing, adapter, and retrieval evidence records
   contain the higher-level restart and end-to-end scenarios.
