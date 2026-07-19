# Hybrid retrieval evidence

Status: **🧱 Foundation (opt-in)**. This is fresh engineering evidence,
not a release, deployment, soak, or production-readiness claim.

## Frozen evaluation

The sanitized corpus contains 17 notes and 16 queries covering identifiers,
filenames, principals, Chinese, English, code-switching, paraphrase, recency,
superseded and disputed statements, mixed visibility, short/long notes, and
prompt-like untrusted text. Thresholds were frozen before model selection.

Selected dense model:

- FastEmbed ID: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
- Hugging Face source: `qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q`
- revision: `faf4aa4225822f3bc6376869cb1164e8e3feedd0`
- license/dimension: Apache-2.0 / 384

Selected sparse model: `Qdrant/bm25`, revision
`e499a1f8d6bec960aab5533a0941bf914e70faf9`, Apache-2.0. The evaluated Jina
Chinese-English candidate was rejected because Recall@5 was 0.89583, below the
predefined 0.90 threshold.

| Evidence | Local | Isolated server 1.18.2 |
|---|---:|---:|
| Recall@5 | 0.92708 | 0.92708 |
| nDCG@10 | 0.88609 | 0.88609 |
| Unauthorized results / scores / snippets | 0 / 0 / 0 | 0 / 0 / 0 |
| Cold query p95 | 94.34 ms | 434.73 ms |
| Warm query p95 | 80.32 ms | 442.26 ms |
| Build time | 0.270 s | 2.403 s |
| Local/server top-5 overlap | 1.0 | 1.0 |

Windows process-tree peak working set was 713,781,248 bytes. The local derived
index was 94,788 bytes. No measured local-to-server trigger fired; therefore
local remains the default documented pilot mode and server mode remains an
explicit connection choice.

Machine results are in `docs/evidence/retrieval-entry-*.json`. The isolated server was
bound only to loopback alternate ports. Its 17-point collection remained green
and complete after stop/start.

## Production scenario evidence

The same scenario pack passed against Qdrant embedded local mode and Qdrant
server 1.18.2. It injects an interrupted rebuild, resumes it, checks the quality
and authorization thresholds, rejects a stale payload against changed Markdown,
performs a changed-corpus safe collection swap, and proves the memory ops
database hash is unchanged throughout.
Two independent hub state directories rebuilding the same corpus against the
same server receive distinct namespaced collections.

Focused contract/provider tests additionally assert that authorization filters
are present in both dense and sparse prefetches and in the outer fused query.
Removing either prefetch filter makes the regression test fail. Provider failure
falls back to keyword search with a sanitized reason.

## Verification levels and remaining limits

- Unit level: deterministic chunk/manifest, metric enforcement, switch-gate, capability,
  packaging, and Skill drift tests.
- Integration level: real multilingual candidate and local/server capability/performance spikes.
- Scenario/replay level: local/server interruption-resume, stale authorization, safe swap, ops
  isolation, and full repository regression.
- Live/soak validation was not run. No soak, remote deployment, cutover, release, or production
  corpus was used.

## Platform and package matrix

| Platform | Python | Full suite | Real local hybrid | Real server hybrid |
|---|---|---:|---:|---:|
| Windows 11 | 3.11.9 | 122 pass / 4 skip | pass | pass (shared server scenario evidence) |
| Windows 11 | 3.12.5 | 122 pass / 4 skip | pass | pass |
| Ubuntu 24.04 / WSL | 3.11.15 | 122 pass / 2 skip | pass | pass |
| Ubuntu 24.04 / WSL | 3.12.3 | 122 pass / 2 skip | pass | pass, including two-state namespace isolation |

Current public-source publication candidate hashes:

- wheel: `7ee04197149bee4b2a91bede66c9c22c345c396612c7432fb5ad44d05266b4c4`
- sdist: `a39f8b57f52306da53cd004338a72a840122efc53f65527ff63cd78349b387d9`

A fresh Python 3.12 wheel environment installed the search, memory-core, and
contracts extras and reported product 0.1.0, qdrant-client 1.18.0, FastEmbed
0.8.0, PyYAML 6.0.3, and jsonschema 4.26.0. Installed Skill validation passed.
With an unavailable explicit server URL, capability discovery reported
`available: false` and an auto search returned the authorized keyword fallback
without exposing the URL.

Independent retrieval ratification used a fresh installed wheel with no
`PYTHONPATH`: the selected hybrid-retrieval contract/provider/local scenario pack
passed 10 tests in 17.415 seconds; and the real Qdrant 1.18.2 server replay
passed in 37.610 seconds. After the public-context rewrite, the rebuilt wheel
ran the complete 122-test suite with official contract dependencies: 118 passed
and 4 expected platform/selected-provider tests skipped in 70.665 seconds. The
public-hygiene scan returned zero hits, all schema JSON parsed, the
self-contained site parsed with 28 reveal nodes and zero external
script/image/link assets, and `git diff --check` reported no errors.

On 2026-07-20, a fail-first public-contract regression exposed that the
retrieval schema still used a placeholder example-domain `$id`. The schema now
uses its canonical GitHub Pages URL, the operator Skill fingerprint was updated,
and the dependency-complete Python 3.12 suite passed 122 tests with 4 expected
selected-provider/platform skips in 91.506 seconds. The focused schema and Skill
contract pack passed 8/8, and the public-hygiene scan remained at zero hits.
