# Adapter Contract

Adapters connect A2A Superhub to a local agent runtime. They translate between
hub tasks/events/artifacts and whatever protocol the runtime already exposes.

## Required operations

| Operation | Purpose |
|---|---|
| `capabilities()` | Return identity, skills, supported intents, limits, and transport notes. |
| `submit_task(task)` | Accept a hub task and start or queue local work. |
| `get_task(task_id)` | Return local mapped status. |
| `cancel_task(task_id)` | Best-effort cancellation or interruption. |
| `stream_events(cursor)` | Return progress and terminal events. |
| `put_artifact(manifest, bytes)` | Deliver an artifact to the local runtime. |
| `get_artifact(ref)` | Fetch local artifact bytes for hub storage. |
| `health()` | Report liveness, version, and degraded reasons. |

## Transport options

Adapters can be implemented several ways:

- CLI wrappers for simple deterministic actions.
- Stdio protocols such as ACP or MCP.
- HTTP or WebSocket sidecars.
- Native modules inside a host runtime.

The hub should prefer existing stable surfaces before asking an agent runtime to
accept patches.

## Recommended mappings

| Local runtime shape | Primary adapter path |
|---|---|
| ACP-capable agent | Map hub tasks to ACP session/prompt/cancel and stream ACP updates. |
| MCP-capable tool surface | Use MCP for resources, events, permissions, and auxiliary tools. |
| CLI-only agent | Use allowlisted wrapper scripts and parse result JSON/artifacts. |
| SDK-based agent | Keep the SDK inside the adapter; expose only hub contract outward. |

## Event mapping

Adapters should emit hub events as soon as meaningful local milestones happen:

- `task.accepted`
- `task.progress`
- `task.input-required`
- `task.result`
- `task.error`
- `task.canceled`

Terminal events should set the task state to `completed`, `failed`, `canceled`,
or `rejected`.
