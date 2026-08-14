# Architecture

## What this repo is

I built DXL Commerce Agent as a runnable, extensible commerce-support Agent Runtime. The code keeps four things separate:

1. intent planning;
2. authoritative business facts;
3. deterministic permission and policy decisions;
4. delivery reliability and inspectable execution state.

The default local configuration uses synthetic fixtures and built-in Browser/Mobile connector simulators, so the complete pipeline can run without a real account. Production source, customer data, credentials, private platform adapters, and deployment details are deliberately excluded; the Connector and Tool interfaces are the integration points for authorized deployments.

## What the current code actually does

```mermaid
flowchart TB
    C[POST /v1/messages] --> API[Strict Pydantic request validation]
    API --> K[Scoped message and session keys]
    K --> L[In-process session lock]
    L --> D{SQLite message claim}
    D -- completed --> CR[Return cached response as deduplicated]
    D -- in flight --> CF[409 plus Retry-After]
    D -- new / expired --> H[Load bounded redacted history]
    H --> X[Redact current message]
    X --> RP[Deterministic rule planner]
    X -. optional .-> OP[OpenAI-compatible planner]
    RP --> I[DecisionPlan: structured intent and entities]
    OP --> I
    I --> RD[Deterministic runtime dispatch]
    RD --> RT[Scoped synthetic read tools]
    RD --> PG[Deterministic refund policy]
    PG --> AS[(SQLite action / approval / execution state)]
    RT --> F[Deterministic reply template]
    AS --> F
    RD --> TR[(Redacted trace and selected audit events)]
    F --> PR[Pydantic response serialization]
```

This is **structured intent plus deterministic runtime dispatch**, not native Function Calling. Neither planner sends a tool call to the runtime. Instead, it returns a `DecisionPlan`; trusted Python code maps the intent to a fixed path and supplies trusted tenant/store/customer context to the tool methods.

The optional model is an intent planner, not an autonomous tool executor or response writer. Its output is parsed into known intent/entity fields. Transport or parse errors fall back to the deterministic rule planner. The final customer response is currently composed by deterministic templates.

## Built-in Browser and Mobile pipeline

```mermaid
flowchart LR
    BC[Synthetic Browser Connector] --> CI[ConnectorIngestor]
    MC[Synthetic Mobile Connector] --> CI
    CI --> IS[(SQLite inbox + cursor)]
    IS --> CW[ConversationWorker]
    CW --> AR[Single Agent Runtime]
    AR --> AC[(Runtime decision / trace cache)]
    AR --> CW
    CW --> OB[(SQLite outbox)]
    OB --> DW[DeliveryWorker]
    DW --> BC
    DW --> MC
    BC --> RR[Synthetic receipt lookup]
    RR --> DW
    DW --> HS[(Handoff state)]
    CS[ConnectorSupervisor] -. health snapshot .-> BC
    CS -. health snapshot .-> MC
```

The connector binding supplies tenant, store, and channel identity outside each synthetic event. Ingestion verifies that scope, redacts text, and atomically commits a poll batch with a cursor compare-and-swap. `ChannelStore` retains normalized payload and connector-binding fingerprints, exposes only the oldest claimable row in each session, and uses local SQLite inbox/outbox records with 60-second fenced leases renewed every 20 seconds by active workers.

`ConversationWorker` invokes the same runtime described above. Runtime decision/trace/cache finalization is one transaction; inbox completion plus deterministic outbox insertion is a later, separate transaction. A tested crash between them replays the cached decision without duplicating turns, then inserts the same outbox key.

`DeliveryWorker` verifies the persisted binding snapshot, payload hash, session generation, and returned delivery key. It records confirmed synthetic Browser delivery, reconciles a simulated Browser timeout-after-commit through its in-memory receipt, retries Mobile only before a remote attempt, and sends an ambiguous or expired non-idempotent result to handoff without a blind retry. Explicit customer handoff increments local session generation, freezes queued automatic replies, enqueues one acknowledgement, and parks later inbound items. The two workers share a local per-session send lock; there is no real channel, cross-process send barrier, distributed queue, operator console, or production Watchdog. See [the complete customer-service architecture](customer-service-architecture.md).

## Implemented components

### FastAPI ingress

`POST /v1/messages` validates a strict envelope containing tenant, channel, store, customer, message, text, and synthetic evidence IDs. Unknown request fields are rejected. In the default local configuration, the endpoint is unauthenticated and expected to remain on loopback; an authorized channel integration must supply authenticated, server-side identity and scope.

Trace, approval, and execution endpoints require `X-DXL-Operator-Key`. This is a shared local key checked with constant-time comparison. It is not an identity provider, role model, tenant authorization system, or production authentication design.

### Scoped keys and message deduplication

Session and message keys are SHA-256 hashes of canonical JSON tuples. Tenant, channel, store, customer, and message identifiers are kept as separate tuple elements so attacker-controlled delimiters cannot make two scopes collide.

Within one process, messages for the same session acquire the same `asyncio.Lock`; unrelated sessions may proceed concurrently. Before planning, the runtime atomically claims the scoped message key in SQLite. A completed replay returns the stored response with `deduplicated=true`; another worker encountering an active claim fails fast and the API returns `409` with `Retry-After: 1`. A claim abandoned by a crashed worker can be reclaimed after its 60-second lease expires.

The local lease protects one exact message ID when processes share the same SQLite file. It does not order different message IDs across processes, renew a long-running HTTP claim, provide a distributed queue, or coordinate multiple hosts. A real multi-host deployment still needs those pieces.

### Redaction and bounded history

The runtime loads a bounded number of recent turns. Phone numbers, email addresses, and token-like strings are redacted before user text is persisted. The **current message is also redacted before either planner receives it**, including in OpenAI-compatible mode. Recent history supplied to the model comes from that redacted store and is further length-bounded.

The built-in redactor intentionally covers a bounded set of patterns; it is not a complete DLP system. Addresses, names, arbitrary identifiers, images, and many secret formats require broader production controls.

### Planners

Both planners implement the same asynchronous contract:

- **RuleBasedPlanner** deterministically classifies a bounded set of built-in Chinese/English phrases and extracts synthetic order/SKU identifiers.
- **OpenAICompatiblePlanner** calls a configurable chat-completions endpoint for a JSON intent/entity decision. Known fields are parsed into `DecisionPlan`; invalid transport, JSON, intent, order ID, or SKU output falls back to rules.

The model adapter does not use native function calling, does not execute tools, and is not exercised by the deterministic offline Eval suite. Authorization never depends on which planner is selected.

### Deterministic runtime dispatcher

The runtime maps each intent to a fixed implementation path:

- greetings, unsupported requests, unknown requests, and detected injection markers return fixed safe responses without tools;
- order status performs a scoped order lookup;
- logistics first performs a scoped order lookup, then uses its trusted order result for shipment lookup;
- product questions perform a tenant/store-scoped SKU lookup;
- refund inquiries return a fixed explanation without reading an order or creating an action;
- refund requests perform scoped order and, when required, evidence lookups before policy evaluation.

For refunds, the runtime requires exactly one synthetic order ID explicitly present in the current customer message and a full match against a closed action-command grammar. Questions, quoted examples, negations, bare amount mentions, and suspicious signed amounts do not enter the write path. Explicit CNY amounts are parsed with `Decimal`, never binary floating point; multiple, signed, over-precise, or oversized amounts fail closed. The runtime re-derives the amount and policy-relevant reason from the message and does not trust the planner's order, amount, or reason fields. This limits the impact of an unsafe or mistaken planner classification.

### Synthetic commerce tools

`CommerceTools` reads immutable synthetic JSON fixtures. Order and evidence lookup require matching tenant, store, customer, and order scope. Product lookup requires matching tenant and store. Logistics lookup is reached only after the dispatcher obtains an order through the scoped order tool.

These are in-process fixture tools, not live commerce adapters, MCP tools, or production APIs.

### Refund policy and side-effect state

The TOML-backed `PolicyEngine` deterministically evaluates:

- existing pending/succeeded refund state;
- positive amount;
- verified paid amount;
- configured maximum;
- required verified synthetic evidence;
- the automatic-approval limit.

The result is `allow`, `deny`, or `require_approval`. An allowed proposal is stored in `approved` state; a higher allowed amount is stored as `pending_approval`; a denial creates no action.

One SQLite action is retained per scoped refund business key `(tenant, store, customer, order)`. Repeated messages proposing another refund for that order reuse the persisted action rather than creating another one.

### Approval and sandbox execution

Approval changes a pending action to `approved`. Execution requires both the configured local operator key at the API boundary and a caller-provided `Idempotency-Key`. SQLite uses `BEGIN IMMEDIATE`, state checks, and unique indexes so the included concurrency test shows two connections cannot execute the same action twice.

Reusing the original idempotency key returns the recorded result. Trying a different key after success returns a conflict. The backend is a synthetic local executor that returns a synthetic refund ID; there is no real payment or platform call.

This code does **not** reconcile an uncertain timeout against a real external backend. That has to be added before enabling real writes.

### Trace and audit data

Each new handled message persists a structured trace containing the hashed public session ID, intent, status, tool steps, policy/action steps, selected public arguments, latency for local planner/tool calls, and named grounding fields. SQLite also records selected audit events for trace creation and action proposal/approval/execution.

This provides inspectable local execution records for debugging and Eval, but it is not an immutable enterprise audit system. There is no trace search UI, external telemetry pipeline, signed log, retention policy, or role-based audit access.

### Response construction

Customer replies are deterministic templates filled with tool and policy results. FastAPI validates the response shape. There is no separate semantic checker that proves every sentence against its source. That becomes important if model-written replies are added later.

## Exact request lifecycle

1. FastAPI validates a synthetic request envelope.
2. The runtime derives collision-resistant scoped message and session keys.
3. The session acquires its in-process lock.
4. The runtime atomically claims the message in SQLite; a completed duplicate is replayed, an active duplicate receives a retryable conflict, and an expired claim may be reclaimed.
5. The runtime loads bounded, already-redacted history.
6. The runtime redacts the current message before invoking the selected planner.
7. The planner returns a structured `DecisionPlan`.
8. Trusted runtime code selects the fixed path and supplies scoped tool arguments.
9. Read tools return facts from synthetic fixtures.
10. A refund request enters deterministic policy evaluation and may create or reuse one sandbox action.
11. A deterministic template constructs the reply.
12. The redacted user turn, assistant turn, trace, selected audit event, cached response, and completed claim state are committed in one SQLite transaction. On an exception, the owning claim is released.
13. Approval and execution, when requested later, use separate operator-key-protected endpoints and SQLite state transitions.

## Implemented invariants

The tests and Eval exercise these current invariants:

- transaction replies use values returned by synthetic tools;
- model output never directly invokes a tool or side effect;
- policy-relevant refund facts are not accepted blindly from a planner;
- scoped order/product/evidence lookups do not return another fixture customer's or store's data;
- exact message replay returns one decision;
- an active cross-connection duplicate fails fast, while an expired message claim can be reclaimed;
- one scoped order has one persisted refund workflow;
- an approved action cannot execute twice, including across two SQLite connections;
- messages in one process are serialized by session;
- current messages and stored history redact the built-in phone/email patterns;
- operator endpoints reject missing or incorrect shared local keys.
- synthetic connector identity fields come from immutable bindings rather than untrusted event metadata;
- connector event/cursor persistence and inbox-completion/outbox insertion are locally atomic;
- stale poll cursors and same-ID changed payloads fail closed;
- stale inbox/outbox lease holders are fenced;
- active worker leases renew and same-session inbox claims remain head-first in SQLite;
- outbox binding, payload, session-generation, and returned delivery-key mismatches cannot confirm delivery;
- Browser timeout-after-commit is reconciled without a second remote attempt;
- ambiguous or expired non-idempotent Mobile delivery is held for explicit resolution without blind retry;
- local human-active sessions freeze queued replies and park later synthetic inbound items.

These are properties of this bounded synthetic implementation, not claims of production exactly-once delivery, comprehensive privacy protection, or distributed tenant isolation.

## Why one agent loop

Intent recognition and the relevant business action share a small context in the current runtime. Adding multiple agents would add coordination surface without creating a stronger permission boundary. Independent production workloads—such as proactive logistics monitoring, refund audit, or knowledge maintenance—could later become separate workers with explicit events and permissions.

## Where RAG could fit

RAG is intentionally absent from the transaction path:

```text
mutable structured facts -> authenticated order/logistics APIs
hard constraints          -> versioned policy code/configuration
unstructured guidance     -> optional read-only document retriever
```

A future retriever should return source, tenant, version, and effective-date metadata. Retrieved text remains untrusted evidence and must not grant authority. The current repository does not implement this retriever.

## What works here and what real use still needs

| Area | What works in this repo | What a real deployment still needs |
|---|---|---|
| Ingress | Strict HTTP schema plus synthetic Browser/Mobile connector protocol and constructor bindings | Authenticated/signed real-channel adapters, rate limits, replay windows |
| Planning | Rules plus optional structured OpenAI-compatible intent planner | Provider timeout/retry policy, budgets, rollout and model monitoring |
| Dispatch | Fixed intent-to-code paths | Versioned tool registry only if the domain requires it |
| Data | Scoped synthetic fixture lookups | Least-privilege tenant-scoped service APIs |
| Channel queue | Local SQLite cursor CAS, head-of-session inbox claims, renewable fenced leases, outbox, and bounded retries | Managed partitioned queue, distributed leases, and multi-host session ordering |
| Ordering | In-process per-session runtime lock plus fail-fast exact-message claim | Distributed per-session ordering |
| Delivery | Binding/payload/key verification, synthetic Browser receipts, and non-idempotent ambiguity handoff | Durable provider receipts and authoritative real-channel reconciliation |
| Handoff | Persistent generation, local send lock, acknowledgement, freezing/parking, resolution-gated release | Cross-process send barrier, authenticated human console, roles, SLA, notifications, and audited resume |
| Writes | SQLite state machine and sandbox idempotency | Authoritative backend operation IDs and ambiguous-result reconciliation |
| Privacy | Basic phone/email/token redaction | Reviewed DLP, encryption, deletion/retention and provider governance |
| Responses | Deterministic grounded templates and API shape validation | Semantic grounding validator if model-generated replies are introduced |
| Observability | Local trace, selected SQLite audit events, and connector health snapshot | Production Watchdog, structured telemetry, access control, retention, alerting, incident workflow |
| Evaluation | 50 deterministic synthetic offline cases | Real failure sampling, adversarial sets, online monitoring, and human review |

Infrastructure should be added in response to measured reliability, scale, privacy, or governance needs—not merely to add framework vocabulary.
