# Read-only troubleshooting

1. Capture target, product version, health, readiness, and structured capabilities.
2. Distinguish connection failure, authentication failure, scope denial, version mismatch, and advertised degraded state.
3. When `operationalDiagnostics` is advertised and the principal has `hub.admin`,
   capture payload-free store counts, source/index revisions, `lagRecords`, queue
   depth, quarantine, retrieval model identity, state bytes, and product version.
4. Sanitize tokens, note bodies, private paths, and real user data.
5. State whether evidence is contract/static, integration, scenario, or soak altitude.

Do not repair, reindex, rotate credentials, migrate, restore, or delete state as
part of diagnosis. If a requested diagnostic endpoint is absent in v1, report the
capability gap instead of guessing from local files.

If an approved offline operation reports an active state lease, stop. Identify the
running hub process and arrange an explicit maintenance window; never bypass or remove
the lock file. A backup public-target refusal means sensitive state was detected, not
a scanner malfunction. A migration parity failure leaves the active provider unchanged.
