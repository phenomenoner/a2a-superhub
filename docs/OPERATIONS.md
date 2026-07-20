# Operational controls

A2A Superhub 0.2 adds local, explicit controls for authoritative backup and
clean restore, recoverable retention, payload-free diagnostics, and Qdrant
local-to-server migration. These controls do not turn a source checkout into a
managed service: operators still own scheduling, encrypted custody, monitoring,
capacity limits, and deployment rollback.

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

Repeated lag checks reuse fully validated notes while their file modification
time and size remain unchanged. A new or changed Markdown file is parsed and
validated again before it can affect the reported lag, and process startup begins
with an empty cache. The HTTP server shares that validated cache with operational
diagnostics, and each diagnostic sample inventories state files in one pass.
This keeps monitoring responsive without treating cached derived data as
authoritative or skipping path and symlink validation on later scans.

Filesystem convergence also computes index-recovery work before opening a write
transaction and creates delivery records only for notes whose authoritative
revision changed. Unchanged corpus entries are not rewritten on every watcher
cycle. SQLite connections use a bounded busy wait for residual cross-process
contention, so a concurrent API note or inbox cursor operation waits for a short
transaction instead of being crowded out by a corpus-wide maintenance write.
Contention that exceeds the bound remains an explicit failed operation; the hub
does not silently discard the write.

## What is authoritative

| State | Backup treatment | Restore treatment |
|---|---|---|
| Task/event SQLite state | included | restored and read directly |
| Artifact manifests and content-addressed blobs | included | restored and checksum-verified |
| Markdown memory notes | included | restored as source of truth |
| Delivery cursors, jobs, receipts, and retention tombstones | included | restored with their SQLite state |
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

A note with any unacknowledged delivery is retained. Private/direct objects need
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
earlier cause with a less specific transport error.

Operational readiness is a property of a tested commit and workload, not a
version string. Consult the repository's operational evidence page before
using that phrase, and preserve any stated workload or platform limitations.
