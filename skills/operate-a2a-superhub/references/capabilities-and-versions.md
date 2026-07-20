# Capabilities and versions

The machine authority is `compatibility.json`; do not infer features from a
planning or marketing label.

The current opt-in memory, hybrid-retrieval, artifact-text, and agent-protocol contract pins:

- product baseline: 0.1.0;
- memory API: `memory.v1`, with opt-in offline sharing implemented;
- note schema: `a2a-superhub.memory.note.v1`, implemented for Markdown notes;
- A2A `Part` mapping: `text`, `raw`, `url`, and `data` oneofs are implemented;
  legacy `kind` input requires an explicit compatibility flag, while the complete
  normative A2A 1.0 JSON-RPC binding is still not implemented;
- artifact API: `artifacts.v1` with base64 compatibility, raw binary, and resumable
  chunk transports; the advertised `maxArtifactBytes` is a guardrail, not a capacity claim;
- derivation: default off, with bounded local PDF text extraction and an optional
  Tesseract executable for image OCR; all output is `untrusted-data`;
- MCP negotiation: protocol `2025-11-25`, implemented by the stateless stdio
  sidecar with ten tools and `memory://note/{id}` plus
  `memory://wakeup/{agent}` resources;
- legacy JSON-RPC coordination: implemented and separately identified.

Treat `memoryFoundation`, `memorySharing`, `timelineGraph`, `safeWakeup`,
`adapter`, `runtimeWatcher`, and `taskLog` as independent granular capabilities.
Treat `memorySearch`, the retrieval mode/version, and `fallbackReason` as
independent search signals. Hybrid means dense+sparse RRF with recency and
authorization pushdown; keyword is the compatible fallback.
`memoryFull` remains false. Delivery, task-log, and watcher side effects default off;
the running instance must explicitly advertise each enabled feature.

Treat `artifactDerivation`, `artifactUploads`, `maxArtifactBytes`, and
`derivedTextTrust` independently. Derivation additionally requires
`memoryFoundation`, `artifact.read`, and `memory.write`; shared/direct derived
notes require the corresponding share authority. Image media support does not
mean the Tesseract executable is installed, so provider availability must be
reported rather than inferred.

MCP resource subscription is independently negotiated. Use resource-updated
notifications when `resources.subscribe` is true. Otherwise poll the same
authorized resource; do not infer subscription support from the protocol version.

Compare the normalized contract fingerprint before operating. On mismatch,
perform read-only discovery, report the differing product/protocol/schema fields,
and stop unless the server advertises a safe compatible fallback.

The current response also returns the authenticated principal subject, kind,
token ID, and sorted scopes. This metadata is not authorization by itself, but
the adapter must match it to its intended principal and the server remains the
final authority. A missing current capabilities route may downgrade to legacy
read-only discovery. Authentication, connection, and transient HTTP failures
must never be relabeled as legacy compatibility.
