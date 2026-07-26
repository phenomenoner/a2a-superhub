# Operational controls

A2A Superhub 0.3 provides local, explicit controls for authoritative backup and
clean restore, recoverable retention, payload-free diagnostics, and Qdrant
local-to-server migration. These controls do not turn a source checkout into a
managed service: operators still own scheduling, encrypted custody, monitoring,
capacity limits, and deployment rollback.

## Memory schema upgrade and rollback boundary

Starting 0.3.0, `memory-ops.sqlite` schema version 4 stores logical deliveries,
per-reason compatibility aliases, exact inbox-page cursor membership, and
logical acknowledgement receipts. Startup acquires the exclusive state lease
before inspecting or migrating this database. A schema newer than the running
binary understands is rejected; it is never guessed or rewritten.

Before first starting 0.3.0 against an older state root, stop the current hub and
create and verify an authoritative backup. Until the upgraded state has been
opened, restoring that backup and starting the older package remains a valid
rollback. Once version 4 has been opened or written, recovery is
**forward-only**: keep the version 4 state intact and repair or upgrade with a
version 4-aware binary. Never start a 0.2.x binary against version 4 state.

Version 0.3.x continues to dual-write the older per-reason delivery rows so
read-only 0.2.x discovery and the `/v1` compatibility surface remain observable.
That dual-write is a compatibility aid, not permission to downgrade the state
in place. The earliest allowed removal of the compatibility rows and `/v1`
delivery shape is a separately versioned 0.4.0 change.

## Authority and maintenance boundary

The running hub holds an exclusive state lease. Backup, retention, restore, and
search migration fail closed while that lease is active. Stop the hub cleanly,
confirm the exact state root and destination, and keep the original state intact
until the restored or migrated instance passes representative reads.

Diagnostics are the exception: an authenticated principal with `hub.admin` can
read `GET /v1/operations/diagnostics` while the hub is running. The response is
deliberately payload-free. It reports counts, queue/quarantine state, the number
of authoritative notes lagging their derived index, index revision, retrieval
model identity, state bytes, and product version without task bodies, note text,
bearer tokens, or local paths.

Filesystem convergence reuses fully validated notes while their file
modification time and size remain unchanged. A new or changed Markdown file is
parsed and validated again before it can affect the completed source/index
snapshot, and process startup begins with an empty cache. Search and operational
diagnostics read that last completed snapshot instead of waiting for an
unrelated convergence scan. Completed API writes advance it immediately. This
keeps serving responsive without treating the derived index as authoritative or
skipping path and symlink validation on later scans.

Only one full diagnostic refresh inventories state files at a time. If another
authenticated request arrives during that refresh, it receives a copy of the
last completed payload-free diagnostic snapshot rather than starting a second
inventory. That cached response retains its original `generatedAt` timestamp,
so monitors can distinguish it from newly collected diagnostics and can alert
if refresh progress remains stale. The source/index fields within a fresh
diagnostic response are separately the last completed convergence snapshot;
they can remain unchanged while a filesystem scan is active. Before the first
diagnostic snapshot exists, a concurrent caller waits for the first collection.

Filesystem convergence also computes index-recovery work before opening a write
transaction and creates delivery records only for notes whose authoritative
revision changed. Unchanged corpus entries are not rewritten on every watcher
cycle. SQLite connections use a bounded busy wait for residual cross-process
contention, so a concurrent API note or inbox cursor operation waits for a short
transaction instead of being crowded out by a corpus-wide maintenance write.
Contention that exceeds the bound remains an explicit failed operation; the hub
does not silently discard the write.

The disposable FTS/KG/timeline index runs in SQLite WAL mode. An active derived
index writer therefore does not block keyword searches from reading the last
committed snapshot, and readers never observe the writer's uncommitted rows.
Startup initialization migrates an existing derived index from rollback-journal
mode. This changes concurrency behavior only: Markdown is still authoritative,
and the entire index remains safe to rebuild.

Only one corpus convergence scan is planned at a time, but the scan does not
hold the cache/catalog lock for its full duration. Search can continue reading
and authorizing unchanged notes while convergence inventories other files; each
changed file still receives full parse, path, and authorization validation before
it can become visible. This is a responsiveness boundary, not a throughput or
latency promise.

If an HTTP peer closes before a response is written, the server treats that
connection as finished and does not try to write a second error response to the
same socket. The accepted operation and authoritative state remain governed by
their normal idempotency and durability rules; a disconnected caller must use
its idempotency key or a read-back to resolve an uncertain response.

## What is authoritative

| State | Backup treatment | Restore treatment |
|---|---|---|
| Task/event SQLite state | included | restored and read directly |
| Artifact manifests and content-addressed blobs | included | restored and checksum-verified |
| Markdown memory notes | included | restored as source of truth |
| Logical deliveries, compatibility aliases, exact-page cursors, jobs, acknowledgement receipts, and retention tombstones | included | restored with their SQLite state |
| Requested principal registry | included as secret | restored under `config/principals.json` |
| FTS/KG SQLite index | excluded as derived | rebuilt from Markdown |
| Local/server Qdrant collections and rebuild markers | excluded as derived | rebuilt by an explicit migration/reindex operation |

## Create a private backup

Stop the hub, then run:

```bash
a2a-superhub --state ./state operations backup create \
  --destination ./private-backups/superhub.zip
```

Add `--auth-config ./principals.json` only when credential recovery is part of
the requested backup. The archive has an exact member manifest with byte counts,
SHA-256 values, classifications, excluded-derived-state declarations, and an
archive SHA-256 in the command result. Existing destinations and symlinked
authoritative files are refused.

`--target-class public` does not sanitize content. It is a guard that refuses
private or secret state. `--allow-sensitive-public` is an exceptional explicit
override that records a warning in the manifest; the resulting archive remains
sensitive and must not be published merely because the command completed.

## Restore to a clean target

```bash
a2a-superhub operations backup restore \
  --archive ./private-backups/superhub.zip \
  --target-state ./restored-state
```

The target must not exist. Restore rejects duplicate or unexpected archive
members, unsafe paths, invalid manifest entries, size/hash mismatches, and
artifact checksum failures. It stages the complete state, rebuilds the derived
memory index, and only then makes the target directory visible. It never
overwrites the source state in place.

Start the restored target on a separate loopback port, run diagnostics, and read
representative tasks, artifacts, notes, and inbox cursors before cutover.

## Recoverable retention

Retention is a recoverable move with a durable tombstone, not hard deletion:

```bash
a2a-superhub --state ./state operations retention trash-note mem_...
a2a-superhub --state ./state operations retention trash-artifact art_...
a2a-superhub --state ./state operations retention list
a2a-superhub --state ./state operations retention restore memory-note mem_...
```

A note with any unacknowledged logical delivery is retained. Private/direct objects need
the separate `--allow-private` authority flag. An artifact manifest cannot be
trashed while authoritative memory references it, and its content-addressed blob
is retained. Restore verifies the tombstoned file hash and refuses to overwrite
an existing destination.

This surface does not erase Git history, external copies, old backups, or shared
content-addressed blobs. General garbage collection and hard delete are not
implemented.

## Qdrant local-to-server migration

Prepare a JSON file whose `queries` array contains sanitized query text,
principal IDs, and optional limits. With an isolated Qdrant server ready:

```bash
a2a-superhub --state ./state operations search-migration drill \
  --server-url http://127.0.0.1:6333 \
  --queries ./parity-queries.json \
  --search-cache-dir ./model-cache \
  --parity-min 1.0 \
  --activate
```

The drill rebuilds both derived providers from authoritative Markdown and
compares ordered results after final per-principal authorization. Activation is
written only after the threshold passes. Run the hub with
`--search-mode configured` to consume that provider. To return to the recorded
previous provider, stop the hub and run:

```bash
a2a-superhub --state ./state operations search-migration rollback
```

The server URL must be explicit HTTP(S) without embedded credentials, query, or
fragment. A parity failure leaves the active provider unchanged.

## Verification harnesses

`tools/single_hub_soak.py` runs concurrent real HTTP task, note, search, inbox,
artifact, and PDF-derivation traffic together with direct authoritative Markdown
edits through alternating clean restarts and controlled process kills. Its
default duration is 24 hours. The final audit reconciles every accepted logical
write with authoritative state and every expected shared-note delivery with the
consumer acknowledgement cursor. Sanitized evidence also reports authorization
checks, queue/quarantine state, RSS and state-size bounds, restart counts, and
the tested commit. The evidence records the exact operation, artifact, restart,
and resource-sampling intervals so readers can distinguish an endurance workload
from a throughput benchmark.

The restart interval is stable serving dwell time: after a replacement child
reports ready and passes its authorization read-back, the next restart deadline
is measured from that completed startup. A slow recovery never causes the
harness to issue immediate catch-up restarts back-to-back.

The harness gives each replacement child up to 60 seconds to complete startup.
This is a recovery budget, not a live-request latency allowance: ordinary
operations retain their 30-second connection retry window. A request that
overlaps a harness-owned controlled restart may use the measured recovery
window, and that extension ends as soon as the replacement reports ready.

`tools/release_gate.py` builds wheel and source archives for the current and
specified previous revision. Independent empty environments install and exercise
the current wheel, current source archive, and packaged Skill before a separate
stateful path verifies upgrade from the previous version, authoritative
backup/restore, package and Skill rollback, and a forward upgrade. Every evidence
check is computed from those probes; no successful check is a fixed constant.
Child processes ignore ambient `PYTHONPATH`, `PYTHONHOME`, and user-site packages
so a source checkout cannot masquerade as an installed release artifact.
Neither harness publishes a package or deploys a hub.

If a workload worker or sample fails, the soak harness preserves that classified
failure while attempting a final delivery, authorization, queue, and resource
audit. A separate finalization failure is additive; it does not replace the
earlier cause with a less specific transport error. Connection deadlines name
the operation that exhausted its retry window, such as `operation-timeout:search`
or `operation-timeout:note-create`, so operators can distinguish the affected
surface without publishing raw process output.

The harness treats three consecutive samples with the same diagnostic
`generatedAt` value as a stale-refresh failure. During finalization it drains
deliveries while the child is available, stops the child, and then reads queue,
quarantine, and terminal-outbox counts directly from the stopped authoritative
databases. A stale or disconnected HTTP diagnostic response is therefore never
substituted for the final exact queue audit.

Each child hub launch appends its process output to `server.stdout.log` and
`server.stderr.log` inside the harness workspace. An unexpected child exit is
reported immediately as `hub-process-exited` instead of being flattened into a
connection deadline. The raw process logs are operator-private diagnostics:
they can contain local paths or dependency messages and must not be copied into
published evidence. Publish only the sanitized JSON result after independently
validating its schema and claims.

Operational readiness is a property of a tested commit and workload, not a
version string. Consult the repository's operational evidence page before
using that phrase, and preserve any stated workload or platform limitations.
