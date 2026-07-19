# Read-only troubleshooting

1. Capture target, product version, health, readiness, and structured capabilities.
2. Distinguish connection failure, authentication failure, scope denial, version mismatch, and advertised degraded state.
3. Capture source/index revisions, queue depth, delivery backlog, and quarantine counts only when the server advertises those fields.
4. Sanitize tokens, note bodies, private paths, and real user data.
5. State whether evidence is contract/static, integration, scenario, or soak altitude.

Do not repair, reindex, rotate credentials, migrate, restore, or delete state as
part of diagnosis. If a requested diagnostic endpoint is absent in v1, report the
capability gap instead of guessing from local files.
