# A2A compatibility contract

Normative target: A2A protocol 1.0 protobuf (`specification/a2a.proto`). Test
parser: official Python `a2a-sdk==1.1.1`, latest observed from the live package
index on 2026-07-19. Generated JSON artifacts and local schemas are not treated
as normative A2A proof.

The shipped v1 facade is legacy and remains so until a real compliant binding
and scenario/replay-level interop pack exist.

| Surface | Shipped legacy v1 | A2A 1.0 contract | Current disposition |
|---|---|---|---|
| Agent Card | custom `a2a.agent-card.v1` | `/.well-known/agent-card.json`, `supportedInterfaces`, product `version` | Official SDK parses canonical fixture; runtime still legacy. |
| Protocol version | not negotiated | each interface declares `protocolVersion: 1.0` | Required on new surface. |
| Send | JSON-RPC `message/send` with principal-derived sender and Part normalization | operation `SendMessage` mapped by binding | The legacy facade accepts the official message/Part shape, but remains a legacy binding. |
| Get task | JSON-RPC `tasks/get` | operation `GetTask` | Legacy method remains separately advertised. |
| Cancel task | JSON-RPC `tasks/cancel` | operation `CancelTask` | Legacy method remains separately advertised. |
| Streaming | absent | `SendStreamingMessage` / `SubscribeToTask` when advertised | Not advertised until ordered event scenario/replay-level evidence. |
| Part | official member names are validated; legacy `kind` requires an explicit compatibility flag | protobuf oneof/member-name JSON: `text`, `raw`, `url`, or `data` | Runtime and official-SDK fixtures cover every member; raw Parts above 256 KiB map to private CAS references. |
| Task ordering | second-resolution event timestamps | monotonic task/event semantics required by product contract | Durable monotonic ordering is implemented for the internal task/event store; standards-binding work remains in coordination hardening. |
| Errors | project JSON-RPC envelope | binding-specific A2A errors | Mapping contract precedes implementation. |

`tests/contracts/fixtures/a2a/agent-card.json` and `send-message.json` are parsed
directly into official protobuf message classes. The Agent Card declares both a
new, currently unimplemented A2A 1.0 interface and a memory extension. It is a
contract fixture, not the card served by v1.

The extension URI maps to `docs/ext/shared-memory/v1.md`. Publication at its
canonical HTTPS URL is a release/publication prerequisite; local existence and
version mapping are checked locally. Publishing the canonical endpoint does not
by itself establish full A2A 1.0 runtime interoperability.
