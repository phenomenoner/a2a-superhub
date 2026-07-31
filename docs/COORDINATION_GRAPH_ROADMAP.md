# Coordination Graph Roadmap

Status: **🗺 Planned** — product boundary and staged roadmap. No coordination-graph
API, plan executor, or orchestration runtime is implemented.

Last updated: 2026-07-31

## Decision

A2A Superhub should **not** reposition itself as a general-purpose "Graph
Engineering platform." That category usually implies ownership of the executable
workflow inside an agentic system: graph definitions, scheduling, branching,
loops, retries, and worker creation.

Superhub has a narrower job:

> Engineer durable, authorized, recoverable relationships between independent
> agent systems.

The right direction is therefore selective:

- **GO:** make the existing coordination relationships observable and explicit;
- **GO, after observation:** validate bounded coordination plans without running
  them;
- **CONDITIONAL:** execute a small set of deterministic cross-agent coordination
  patterns only after demand and failure evidence justify the responsibility;
- **HOLD:** dynamic routing, runtime-generated graphs, and bounded loops;
- **NO:** absorb peer runtimes, manage their internal prompts or model loops, or
  become a general workflow engine.

A separate orchestration plane is not an approved product surface. The planned
work is a **coordination-graph extension** to the existing coordination plane.
It earns a separate plane only if the staged evidence shows that execution—not
just traceability—is a recurring cross-runtime need.

## Three different graphs

The word "graph" names three different things in this product area. They must not
be conflated.

| Graph | What the nodes and edges mean | Superhub status |
|---|---|---|
| **Knowledge graph** | Notes, agents, projects, tasks, artifacts, and typed memory relations | Implemented as an authorized, derived 1–2 hop memory view. It is not an execution graph. |
| **Coordination graph** | Agents, tasks, handoffs, events, artifacts, deliveries, and their causal relationships | Present implicitly across current stores and contracts; no first-class run/lineage projection exists. |
| **Executable workflow graph** | Steps that a scheduler advances through branches, joins, retries, loops, and approvals | Not implemented and not part of the current product claim. |

## First-principles fit

The product succeeds when heterogeneous peers complete useful work without every
pair inventing a private integration or losing collaboration state.

```text
useful cross-agent completion
= routable work
× receiver reachability
× contract correctness
× durable state fidelity
× recovery after interruption
× authorized observability

net product value
= useful cross-agent completion
÷ (integration coupling + operating burden + central-authority risk)
```

A graph capability belongs in Superhub only when it improves that equation at the
**cross-agent boundary**. It must pass all of these tests:

1. **Cross-runtime value:** it solves a relationship among independent runtimes,
   not a private loop inside one runtime.
2. **Peer autonomy:** agents remain peers, not children of a central framework.
3. **Contract ownership:** the Hub can enforce the behavior through task,
   identity, delivery, artifact, and receipt semantics it already owns.
4. **Adapter boundary:** local execution details remain inside removable adapters
   or peer runtimes.
5. **Evidence after failure:** restart, replay, partial completion, authorization,
   and cancellation behavior can be proved without inferring success from a
   model message.
6. **Optionality:** existing clients and the dependency-free coordination core
   keep their current behavior unless the extension is explicitly enabled.

These tests favor lineage, traceability, validation, and bounded coordination.
They reject an open-ended scheduler that duplicates the harness inside every
peer.

## Scope decision

| Candidate capability | Decision | Why |
|---|---|---|
| Authorized task/run lineage and causal trace | **GO** | Makes existing durable facts diagnosable without taking new execution authority. |
| Read-only agent/capability/topology projection | **GO** | Reduces integration discovery cost if derived from authenticated current facts. |
| Closed coordination-plan schema and side-effect-free validation | **GO after trace** | Tests whether repeatable cross-agent structure exists before building a scheduler. |
| Deterministic dependencies and bounded fan-out/fan-in | **CONDITIONAL** | Useful for recurring multi-peer work, but adds partial-failure and replay responsibility. |
| Timeout, bounded retry, cancellation propagation, and explicit failure policy | **CONDITIONAL** | Belongs with execution only when every transition remains visible and idempotent. |
| Human approval checkpoints before declared side effects | **CONDITIONAL, required before execution** | Preserves authority boundaries; approval text is never treated as authorization by itself. |
| Capability-based route selection and fallback | **HOLD** | Capability freshness, trust, and fallback semantics are not yet strong enough for hidden routing. |
| Runtime-generated graphs and bounded loops | **HOLD** | Requires cardinality, cost, authority, and termination controls beyond the first useful slice. |
| Dynamic creation or hosting of peer agents | **NO** | Violates the peer/runtime boundary and creates a new platform responsibility. |
| Prompt, context-window, model, or internal tool-loop management | **NO** | This is harness ownership, not cross-agent coordination. |
| General-purpose workflow/BPM replacement | **NO** | Expands the category without improving Superhub's differentiated job. |

## Staged roadmap

Later stages are options, not commitments. Each stage must satisfy its own gate;
completion of an earlier stage does not authorize the next one.

### Stage 0 — Boundary and evidence questions

**Status:** documented here; no runtime capability.

- Keep the public position: graph-engineered coordination and shared memory, not
  a general graph orchestrator.
- Gather concrete workflows where current direct tasks, handoffs, and adapters
  require duplicated glue or make failures hard to reconstruct.
- Record the baseline: integration code, handoff completion, duplicate/orphaned
  tasks, recovery behavior, and time to diagnose cross-agent failures.

**Advance gate:** at least two heterogeneous runtime paths expose the same
coordination or diagnosis problem at the Hub boundary. A single framework's
internal workflow is not qualifying evidence.

### Stage 1 — Observe: lineage and causal trace

**Status:** 🗺 Planned. A separate Design RFC and closed schemas are required
before implementation.

Candidate scope:

- first-class run identity and typed task lineage;
- explicit parent/child and dependency facts without overloading free-form
  correlation metadata;
- authorized projections connecting agents, tasks, events, handoffs, artifacts,
  and terminal outcomes;
- a bounded trace query/export that reports facts, not inferred comprehension or
  completion;
- reconstruction from authoritative task/event state after restart.

This stage does **not** schedule work.

**Acceptance gate:**

- every edge has immutable source provenance;
- authorization is applied before traversal and limits, with zero hidden-edge or
  hidden-node leakage;
- restart/rebuild returns the same authorized topology;
- old clients behave identically when the feature is absent or disabled;
- trace output distinguishes created, delivered, accepted, working, terminal,
  unknown, and unavailable facts without collapsing them;
- fixed-corpus latency, storage, and write-amplification budgets are declared in
  the RFC and met by executed evidence.

### Stage 2 — Validate: coordination plans without execution

**Status:** 🗺 Planned after Stage 1 evidence.

Candidate scope:

- a versioned, closed plan schema referencing registered agents, intents,
  dependencies, limits, permissions, artifacts, and approval points;
- deterministic validation against a capability snapshot;
- a side-effect-free dry run with a stable plan digest, immutable plan version,
  and explicit unknowns;
- bounded cardinality; arbitrary cycles and hidden fallback are rejected in the
  first version;
- plans contain coordination metadata, not peer prompts or private harness
  configuration.

**Acceptance gate:**

- validation and dry run create no tasks, deliveries, artifacts, ACKs, or peer
  calls;
- the same plan and capability snapshot produce the same result and digest;
- stale/missing capabilities, unresolvable dependencies, privilege expansion,
  and unbounded fan-out fail closed;
- the plan maps losslessly to ordinary Hub task and policy concepts rather than
  inventing a second task truth.

**Stop option:** if users gain sufficient value from trace + validation, stop
here. A useful design and diagnosis surface does not need to become a scheduler.

### Stage 3 — Execute: bounded coordination patterns

**Status:** conditional; not committed.

Entry requires all of the following:

- Stage 1 and 2 evidence is complete;
- repeated pilots show material value beyond direct task creation and adapter
  glue;
- the A2A 1.0 binding and coordination-hardening work needed by the selected
  pattern are complete;
- execution, incident, cancellation, and upgrade/rollback ownership are named;
- an opt-in/default-off package and scope boundary are approved.

Candidate first slice:

- deterministic dependencies;
- bounded fan-out and fan-in over already registered peers;
- transitions triggered only by typed task/event facts and receipts, never by
  interpreting untrusted payload or artifact text as routing authority;
- timeout, bounded retry, cancellation propagation, and explicit failure policy;
- human approval checkpoints before declared side effects;
- an optional removable worker, while ordinary Hub task/event state remains the
  authoritative execution record.

**Acceptance gate:**

- process-kill and restart tests cover partial construction, partial fan-out,
  fan-in, approval wait, timeout, cancellation, and terminal replay;
- retries do not create duplicate logical child tasks or hide earlier outcomes;
- every resumed run remains pinned to its original plan version and policy;
- a peer completing after timeout or cancellation remains a visible late fact;
  the Hub never rewrites it into success or claims remote execution stopped;
- partial failure remains visible and never becomes an invented aggregate
  success;
- limits cap child count, depth, elapsed time, retries, and artifact/payload size;
- disabling or removing the executor leaves coordination and memory surfaces
  intact;
- at least two heterogeneous runtime adapters complete the same bounded pattern
  without peer-runtime source patches.

### Stage 4 — Decide later: dynamic coordination

**Status:** HOLD; not part of the committed roadmap.

Only after Stage 3 should the project reconsider capability-based routing,
runtime-generated subgraphs, bounded loops, or fallback selection. A peer or
model may propose a graph, but proposal is never authorization. Any future slice
must have explicit cardinality, cost, depth, termination, approval, revocation,
and audit controls.

## Cross-cutting invariants

Any implemented slice must preserve the existing product contract:

- **Peers, not children.** Superhub coordinates independent agents; it does not
  become their host or internal harness.
- **Hub semantics, adapter execution.** Cross-agent truth belongs to the Hub;
  runtime-specific behavior belongs to adapters and peers.
- **One task truth.** A graph view or plan may reference tasks; it must not create
  a competing task lifecycle.
- **Default deny and explicit authority.** Model output, prompt text, graph shape,
  and approval prose are untrusted data until deterministic policy authorizes an
  action.
- **Opt-in and removable.** No graph feature may silently change the current
  coordination-only or memory-enabled runtime.
- **Restart and replay before scale.** Exactly-once claims are avoided; logical
  idempotency and visible at-least-once recovery must be demonstrated.
- **No graph database by default.** Shallow authorized projections should remain
  derived from existing authoritative stores unless measured queries prove a
  different need.
- **No premature platform claim.** "Graph Engineering platform" is earned only
  after a supported executable surface has independent adoption and evidence.

## Measures and stop rules

Each pilot should compare the new slice with current direct tasks and adapters on
the same workflow:

- integration code and runtime-specific glue required;
- cross-agent completion and explicit failure rates;
- duplicate, orphaned, and unknown task counts;
- recovery after Hub, adapter, or peer interruption;
- time to reconstruct a failure from authorized evidence;
- latency, storage, and operator burden introduced by the extension.

Graph, run, and node counts are diagnostic measures, not success metrics. The
decision turns on accepted cross-agent outcomes, recovery, and reduced
integration/diagnosis cost.

Stop or narrow the work when any of these is true:

1. the recurring problem is inside one peer's harness rather than between peers;
2. traceability removes the pain without execution;
3. a workflow framework plus the existing task API solves the case with less
   coupling;
4. the design requires peer-runtime source patches instead of adapters;
5. authorization or replay semantics cannot fail closed;
6. operating burden exceeds the measured reduction in integration and diagnosis
   cost;
7. demand depends on dynamic agent creation or hidden model authority.

## Positioning shorthand

Use this distinction in public discussion:

> Workflow graph frameworks engineer paths inside an agentic system. A2A
> Superhub engineers durable relationships between independent agent systems.

Current defensible description:

> A2A Superhub applies graph-engineering principles to cross-agent coordination
> and shared memory. It is not a general-purpose workflow graph runtime.

## Reference

- LangChain, ["3 Years of Graph Engineering with LangGraph"](https://www.langchain.com/blog/3-years-of-graph-engineering-with-langgraph),
  2026-07-22 — graphs as a balance of deterministic paths and agentic steps;
  full agents can be nodes, while open-ended work may remain better inside a
  peer-controlled agent loop.
