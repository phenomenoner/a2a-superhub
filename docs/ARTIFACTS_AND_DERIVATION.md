# Artifact Transport and Searchable Derived Text

A2A Superhub stores artifact bytes in a SHA-256 content-addressed store. The
manifest and blob are authoritative; derived Markdown and search indexes are
rebuildable outputs. The default maximum artifact size is 64 MiB and can be
changed with `serve --max-artifact-bytes`.

## Enable the optional derivers

```bash
python -m pip install -e ".[memory-core,derive]"
a2a-superhub --state ./state serve --enable-memory --enable-derivers
```

Derivers are off by default. PDF text extraction runs locally through `pypdf`.
Image OCR validates/decompresses through Pillow, then invokes a separately
installed `tesseract` executable with an argument-safe subprocess and timeout.
Provider absence is reported as an error; the hub does not silently send media
to a cloud API.

## Complete raw upload

`PUT /v1/artifacts/raw` accepts a binary body with:

- `Content-Length` (required);
- `Content-Type`;
- `X-Artifact-SHA256` (recommended and verified when present);
- `X-Artifact-Filename`;
- `X-Artifact-Visibility`: `private`, `shared`, or `direct:<principal>`.

The authenticated principal becomes `createdBy`; client-provided ownership is
never trusted. A temporary file is flushed and checksum-verified before the blob
and manifest become visible. Size or checksum failure removes the partial file.
Legacy manifests without an explicit visibility field are treated as private.
Static principal registries must grant `artifact.share` before a caller can
create or change shared/direct artifacts; legacy single-token mode already maps
to the complete local-operator scope set.

## Restart-safe resumable upload

1. `POST /v1/artifacts/chunks` with `sizeBytes`, `chunkSize`, `sha256`, optional
   filename/media type, and visibility.
2. `PUT /v1/artifacts/chunks/<upload-id>/<zero-based-index>` with the exact chunk
   bytes and optional `X-Chunk-SHA256`.
3. `POST /v1/artifacts/chunks/<upload-id>/commit` with `{}`.

Chunks may arrive out of order. Repeating identical bytes at an index is
idempotent; different bytes at the same index are a conflict. Commit fails while
any chunk is missing and verifies the whole-file checksum before CAS admission.
Session metadata survives a server restart. Explicit
`POST /v1/artifacts/chunks/<upload-id>/cancel` removes partial chunks while
retaining a canceled status record.

The JSON `POST /v1/artifacts` base64 route remains available for compatibility,
but raw or resumable upload avoids base64 expansion.

## A2A Part mapping

Message parts accept the official oneof member names `text`, `raw`, `url`, and
`data`; exactly one must be present. `raw` is strict base64. Raw parts larger
than 256 KiB become private CAS URL references and require `artifact.write`.
The old `kind` discriminator is accepted only when the caller explicitly opts
into legacy mapping. This implements Part validation/mapping, not the complete
A2A 1.0 JSON-RPC binding.

## Derive, search, and retrieve

`POST /v1/artifacts/<artifact-id>/derive` with `{}` chooses the registered
provider by media type. The durable job can be read at
`GET /v1/derivations/<job-id>`. A failed or canceled job requires
`{"retry": true}` to retry. The resulting Markdown note includes:

- an `UNTRUSTED DERIVED DATA` boundary;
- source artifact ID and authoritative checksum;
- provider name/version and media type;
- an `x-derived-from` backlink plus the checksum reference.

The extracted text remains data even when it contains phrases such as “ignore
previous instructions.” Adapters and the operator Skill must never promote it
to a system or developer role.

The derived note initially inherits the source visibility. Every note read and
search hydration checks the current source manifest again, so narrowing a source
from shared to private immediately hides stale derived search candidates. A
missing or inconsistent manifest fails closed.

## Cancel and rollback

`POST /v1/derivations/<job-id>/cancel` cancels a pending/running job. Provider
timeouts and output limits bound work; cancellation may complete at the next
provider boundary rather than interrupting a native process instantly.

`POST /v1/derivations/<job-id>/purge` is an admin-only destructive operation.
It removes only the derived Markdown note and rebuildable index entries. It does
not delete or mutate the source manifest/blob. The product exposes no general
source-artifact deletion endpoint.

## Verification and limitations

Repository scenarios cover raw and out-of-order resumable transport, duplicate
and missing chunks, checksum mismatch, restart/replay, explicit cancel, valid and
encrypted/malformed/oversized PDF handling, malformed/oversized images, real
Tesseract OCR when the provider exists, prompt-like text containment, cross-
principal search, current-ACL denial, default-off behavior, and derived-only
cleanup. These are repository and CI outcomes, not production deployment, load,
latency, or soak claims. Image captioning, audio/video transcription, general
artifact garbage collection, and the complete A2A 1.0 binding are not implemented.
