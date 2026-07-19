# Durable memory and sharing performance baseline

Evidence date: 2026-07-19. This is a cold, single-run engineering baseline,
not an SLA, capacity promise, production sizing guide, or long-running soak result.

## What is measured

`benchmarks/memory_foundation_baseline.py` creates real Markdown notes with atomic replacement
on a disposable filesystem, rebuilds a real SQLite/FTS5 derived index, runs a
keyword query, enables delivery and backfills one `about` delivery per note,
then builds a 64 KiB safe-wakeup payload. Each size uses a new state directory;
there is no warmup. The output is atomically checkpointed after every size.

Both final runs imported the rebuilt wheel rather than `src/`. The Linux source
mount was read-only. The measured sizes were 1, 1,000, and 10,000 notes.

## Final-code results

Times are seconds; lower is faster. FTS returns at most 50 hits. Wakeup includes
37 items at the two larger sizes because the whole-item byte budget is enforced.

| Platform | Notes | Write | Rebuild | FTS | Delivery startup | Wakeup |
|---|---:|---:|---:|---:|---:|---:|
| Windows 11 / CPython 3.12.5 | 1 | 0.101 | 0.142 | 0.013 | 0.103 | 0.071 |
| Windows 11 / CPython 3.12.5 | 1,000 | 16.842 | 71.317 | 0.578 | 23.435 | 8.045 |
| Windows 11 / CPython 3.12.5 | 10,000 | 134.934 | 440.451 | 0.213 | 169.529 | 67.005 |
| Linux/WSL2 / CPython 3.12.13 | 1 | 0.041 | 0.125 | 0.006 | 0.081 | 0.030 |
| Linux/WSL2 / CPython 3.12.13 | 1,000 | 8.928 | 6.397 | 0.133 | 9.873 | 3.436 |
| Linux/WSL2 / CPython 3.12.13 | 10,000 | 87.019 | 70.117 | 0.116 | 79.829 | 34.568 |

Machine-readable results:

- `docs/performance/memory-foundation-baseline-windows.json`
- `docs/performance/memory-foundation-baseline-linux.json`

The Linux image is pinned as
`python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de`.
It ran under Docker Desktop's WSL2 kernel on the same 12-logical-CPU host, so it
is not an independent bare-metal comparison. Host filesystem, antivirus,
Docker/WSL2 virtualization, power state, and cache state can materially change
these numbers.

## Reproduction

Install the built artifact with the memory extra, ensure the benchmark imports
that installation, then run:

```text
python -I benchmarks/memory_foundation_baseline.py --sizes 1 1000 10000 --output baseline.json
```

The Windows final run exited 0 in 948.8 seconds. The Linux final run exited 0
in 314.2 seconds. Counts at 10,000 were 10,000 indexed notes and 10,000 durable
deliveries; wakeup returned `role=data` and was byte-budget truncated.

## Excluded and superseded attempts

- An earlier combined Windows invocation exceeded its 15-minute capture limit
  and left a child process. That process was explicitly identified and stopped;
  any overlapping follow-up timings were discarded as resource-contaminated.
- Earlier split Windows and Linux results predated the final receipt-path
  change. They were superseded rather than mixed into this table.
- The first final Linux setup invocation installed the wheel with `--no-deps`.
  It exited 1 before completing a size because PyYAML was absent. The corrected
  run installed `[memory-core]`; the setup failure contributes no timing data.

## Interpretation boundary

The baseline shows that the durable-memory and sharing implementation completes the required real
filesystem/SQLite workflow at all three corpus sizes and exposes where current
cost lies. In particular, cold rebuild, delivery backfill, and safe-wakeup
assembly are materially more expensive than an FTS query. It does not establish
concurrency capacity, tail latency, memory ceilings, durability under sustained
load, or a production SLA. Those require workload-specific live/soak evidence.
