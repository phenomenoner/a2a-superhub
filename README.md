# A2A Superhub

A2A Superhub is a small standalone hub for durable agent-to-agent coordination.
It gives independent agents a shared place to exchange tasks, progress events,
and artifacts without forcing every agent to run inside the same framework.

The project is intentionally modest: it is a dependency-free Python service with
SQLite-backed task state, a content-addressed artifact store, an Agent Card
registry, a JSON HTTP API, and a minimal JSON-RPC A2A facade. It is meant to be
easy to run locally, easy to inspect, and easy to adapt.

## Why this exists

Modern agent stacks are increasingly heterogeneous. One team may use an A2A-capable
service, another may expose MCP tools, another may have an ACP editor adapter,
and another may only have a CLI. A useful coordination layer should not require
all of them to migrate into one runtime.

A2A Superhub separates the concerns:

- The hub owns cross-agent task lifecycle, artifacts, receipts, idempotency, and policy.
- Adapters own local integration with a specific agent runtime.
- Agents remain peers, not children of a central application framework.

## What is included today

- Standalone state root with SQLite task and event storage.
- Agent Card registration and listing.
- Task create/get/list/cancel/event operations.
- Content-addressed artifact storage with SHA-256 verification.
- Optional bearer-token protection for HTTP endpoints.
- Basic per-client rate limiting.
- Minimal JSON-RPC endpoint for `message/send`, `tasks/get`, and `tasks/cancel`.
- Dependency-free command-line interface and server.

## What is intentionally not included yet

- Production adapter implementations for specific agent runtimes.
- Native patches for any existing agent framework.
- A hosted service or multi-tenant control plane.
- Full A2A protocol coverage.
- A policy engine beyond the small bearer-token/rate-limit MVP.

Those pieces belong behind adapters and can evolve without changing the hub's
core task and artifact model.

## Quickstart

```bash
python -m venv .venv
. .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .

a2a-superhub --state ./state init
a2a-superhub --state ./state serve --host 127.0.0.1 --port 8787
```

In another terminal:

```bash
curl http://127.0.0.1:8787/healthz
curl http://127.0.0.1:8787/.well-known/agent-card.json
```

Create a task:

```bash
curl -s http://127.0.0.1:8787/v1/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "fromAgent": "agent.alpha",
    "toAgent": "agent.beta",
    "intent": "agent.query",
    "idempotencyKey": "demo-001",
    "payload": {"summary": "Summarize the attached artifact"},
    "permissions": {"sideEffects": "default-deny", "scopes": ["agent.prompt"]}
  }'
```

Append progress or a terminal state:

```bash
curl -s http://127.0.0.1:8787/v1/tasks/<task-id>/events \
  -H 'Content-Type: application/json' \
  -d '{"kind":"task.progress","state":"working","payload":{"message":"adapter accepted task"}}'
```

Upload an artifact:

```bash
python - <<'PY'
import base64, json, urllib.request
body = json.dumps({
  "filename": "note.txt",
  "mediaType": "text/plain",
  "createdBy": "agent.alpha",
  "contentBase64": base64.b64encode(b"hello from an agent").decode(),
}).encode()
req = urllib.request.Request(
  "http://127.0.0.1:8787/v1/artifacts",
  data=body,
  headers={"Content-Type":"application/json"},
  method="POST",
)
print(urllib.request.urlopen(req).read().decode())
PY
```

## CLI examples

```bash
a2a-superhub --state ./state agent register --file examples/agent-cards/example-agent.json

a2a-superhub --state ./state task create \
  --from-agent agent.alpha \
  --to-agent agent.beta \
  --intent agent.query \
  --summary "What can you do?" \
  --idempotency-key local-demo-001

a2a-superhub --state ./state task list
```

## Minimal JSON-RPC facade

`POST /a2a` accepts a small JSON-RPC 2.0 subset:

- `message/send` or `tasks/create`
- `tasks/get`
- `tasks/cancel`

Example:

```json
{
  "jsonrpc": "2.0",
  "id": "req-1",
  "method": "message/send",
  "params": {
    "fromAgent": "agent.alpha",
    "toAgent": "agent.beta",
    "intent": "agent.query",
    "idempotencyKey": "req-1",
    "payload": {"summary": "hello"}
  }
}
```

## Adapter model

Adapters are expected to translate between Superhub task semantics and a local
agent runtime. A Hermes adapter might use ACP for sessions and MCP for side
surfaces. A local automation adapter might use CLI wrappers. A framework adapter
might use an in-process SDK.

The hub should not need to know how a specific agent thinks, stores sessions, or
runs tools. It only needs durable task events, artifacts, and policy decisions.

See [docs/ADAPTERS.md](docs/ADAPTERS.md) for the adapter contract.

## Security posture

A2A Superhub is local-first. Bind it to loopback by default, use bearer-token
auth when exposing it outside a trusted process boundary, and treat every peer
message and artifact as untrusted input.

See [docs/SECURITY.md](docs/SECURITY.md) for details.

## Development

```bash
python -m unittest discover -s tests
```

The project uses only the Python standard library at runtime.

## License

MIT
