# Production Lessons Behind the Reference Design

## Evidence boundary

This repository is a **fresh, privacy-safe reference implementation**. It turns engineering lessons into newly written code, synthetic fixtures, tests, and Eval cases without publishing the author's private production source, operational data, platform automation, credentials, prompts, or customer records.

The author reports, in aggregate, that the private automation environment supported more than ten stores, six concurrent live rooms, more than twenty hours of generated media per day, and more than ten GPU-equipped machines. The author also reports customer-response times of approximately 0.5–2.5 minutes and an overall labor-cost reduction of about half. These figures are **approximate, self-reported, not independently audited, and not performance claims for this public repository**.

Reproducible evidence in this repository is narrower: 28 unit/integration tests and 50 deterministic synthetic Eval cases, all backed by synthetic fixtures. Those checks demonstrate bounded implementation behavior; they do not validate the private production figures.

## 1. Optimize for resolution, not agent count

A commerce customer wants a correct answer or completed action. Multiple agents discussing one bounded request do not inherently improve correctness. A single decision loop with narrow tools and a deterministic policy boundary is often easier to observe and operate.

The repository therefore uses one planner contract and one runtime. It does not use multi-agent orchestration. A future production system might separate independently scheduled domains—such as proactive logistics monitoring or refund audit—but they should communicate through explicit events and permissions rather than participate in every live reply.

## 2. Separate planning from dispatch

Model-native Function Calling is useful in some systems, but it is not the only way to build an agent. In this implementation, the planner returns a structured `DecisionPlan`; trusted runtime code maps that intent onto a fixed tool path.

That detail matters in a portfolio: this repository demonstrates typed intent planning and deterministic runtime dispatch, **not native function calling**. The optional OpenAI-compatible provider cannot directly name or execute a tool, and deterministic rules remain available without a model key.

## 3. Transaction facts belong in transactional systems

Models should not remember order state, and document retrieval should not guess it. Orders, logistics, payment, inventory, and refund outcomes change and require exact ownership constraints. The reference runtime therefore obtains facts from tenant/store/customer-scoped synthetic tools.

RAG can help with large unstructured corpora such as manuals, policy explanations, or troubleshooting guides. It should be a read-only evidence source rather than a substitute for transaction APIs or an authority to approve a side effect. RAG is not implemented in this repository.

## 4. Policy must survive a bad planner decision

Prompts influence behavior but do not enforce authorization. Refund limits, allowed state, paid amount, evidence requirements, and approval thresholds belong in deterministic code or versioned configuration.

The runtime goes one step further for the demonstrated refund flow: it separates `refund_inquiry` from `refund_request`, requires one order ID and a closed, anchored action command explicitly present in the customer message, and re-derives the amount and policy-relevant reason rather than trusting planner fields. Amount parsing uses `Decimal`, not binary floating point. Questions, quoted examples, negations, bare amount mentions, multiple amounts, suspicious signs, and over-precise values fail closed. Tests inject unsafe planners that attempt to change the order or downgrade the reason and confirm that the runtime preserves the customer-selected order and required evidence.

## 5. Serialize by conversation, not globally

Messages in one conversation may depend on order, while unrelated conversations should not block each other. The current code uses one in-process `asyncio.Lock` for each canonical session key. It also uses an expiring SQLite claim to ensure that two processes sharing the demo database do not both handle the exact same message ID; an active duplicate fails fast, and an abandoned claim can be reclaimed after 60 seconds.

That is enough for deterministic local behavior, not for a horizontally scaled service. The SQLite claim does not order different messages in one session across processes or hosts, and it is not renewed during long work. A durable partitioned queue, renewable distributed lease, or equivalent ordering mechanism is production-next work.

## 6. At-least-once delivery demands several kinds of deduplication

The reference implementation demonstrates three distinct mechanisms:

- a canonical message key plus expiring SQLite claim prevents concurrent handling of an exact redelivery and returns the cached decision after completion;
- a canonical refund business key retains one action per scoped order;
- a caller-provided execution idempotency key returns the recorded result when replayed.

SQLite transaction locking and a unique index also prevent two included demo connections from completing one action twice.

The harder production case is an external write whose result is unknown after a timeout. A real service must reconcile against the authoritative backend operation before retrying. That reconciliation is a design lesson and a **production-next requirement; it is not implemented by this synthetic executor**.

## 7. Redact before the model, not only before logs

The current message is redacted before either planner sees it, including in OpenAI-compatible mode. Persisted recent history is redacted too. This reduces accidental propagation of the phone, email, and token-like patterns covered by the small demo redactor.

Production privacy needs much more: identity-aware data minimization, broad DLP, attachment handling, provider review, encryption, retention, deletion, and audit access controls. A few regular expressions are evidence of the boundary, not a privacy guarantee.

## 8. Memory has several meanings

Recent conversation turns, transaction state, workflow state, and semantic memory are not interchangeable:

- bounded recent turns help carry local conversational context;
- order/refund truth belongs in business storage;
- action state supports approval and idempotent execution;
- semantic retrieval memory is optional and must not become transaction truth.

The repository implements the first three in a small SQLite-backed demo. It does not implement vector memory or RAG.

## 9. Evaluate the trajectory, not only the final sentence

A fluent answer can conceal a wrong tool, unsafe policy outcome, or missing source fact. The offline Eval runner therefore checks structured behavior such as:

- expected intent and final status;
- required and forbidden tool names;
- policy outcome and reason code;
- named facts used in the reply;
- required trace steps;
- duplicate-message behavior;
- absence of selected sensitive fragments in returned/persisted material.

The current result is **50/50 synthetic cases passed with 0 tagged safety failures** under the deterministic rule planner. This is separate from the **28 unit/integration tests**, which directly exercise code contracts and edge conditions. Neither suite measures model cost, token use, real-service latency, customer satisfaction, or production resolution rate.

## 10. Operational metrics need definitions

Statements such as “automatically resolved” or “no failures” are not useful until their denominator, time window, workflow scope, and failure definition are explicit. A production dashboard should distinguish:

- model/tool success from customer issue resolution;
- temporary retry from unrecovered failure;
- planned escalation from silent abandonment;
- message-send success from business-action completion;
- average latency from tail latency.

The public project intentionally does not convert the author's approximate private-system background into a benchmark. Its reproducible measurements are the versioned tests and synthetic Eval cases described above.

## 11. Shared secrets are not operator identity

The demo protects trace, approval, and execution routes with `X-DXL-Operator-Key`. This is useful for preventing accidental open access during a localhost demonstration, but every holder has the same authority and the supplied approver is only an audit label.

Production human approval needs authenticated operator identity, role-based authorization, tenant scope, separation of duties where required, session controls, and durable accountability. The Compose service binds to localhost to keep the demo exposure narrow; localhost binding is not itself authentication.

## 12. Frameworks are replaceable; invariants are not

LangGraph, MCP, Agent SDKs, and model function calling can improve development when their capabilities match a measured need. None automatically provides ownership validation, idempotency, policy enforcement, or auditability.

This repository intentionally implements those ideas with plain Python, FastAPI, Pydantic, and SQLite. Its job is to make the control flow easy to inspect. Framework adoption can follow if workflow complexity, interoperability, or scale justifies it.

## Implemented now vs production-next

| Implemented now | Production-next |
|---|---|
| Structured intent plus fixed runtime dispatch | Native tool protocol only if dynamic discovery is needed |
| Scoped synthetic reads | Authenticated, least-privilege commerce APIs |
| Deterministic synthetic refund rules | Reviewed, versioned business/risk policy and regulatory controls |
| Local message/action dedupe and SQLite idempotency | Durable distributed workflow plus external-backend reconciliation |
| In-process session serialization plus an expiring exact-message SQLite claim | Partitioned durable queue and renewable distributed lease |
| Basic pre-planner and persistence redaction | Comprehensive DLP and lifecycle governance |
| Shared demo operator key | Individual identity, roles, tenant authorization, approval accountability |
| Deterministic templates and API shape validation | Semantic grounding validation if model-written responses are introduced |
| 28 tests and 50-case offline synthetic Eval | Governed production telemetry, adversarial sets, human review, and incident feedback |

## Practical evolution order

For a small team, a defensible order is:

1. reliable scoped tools and deterministic business policy;
2. message/action deduplication and idempotent writes;
3. inspectable traces plus unit/integration tests and behavioral Eval;
4. real identity, authorization, privacy, observability, and deployment controls;
5. durable coordination and external-backend reconciliation;
6. optional RAG for a measured unstructured-knowledge gap;
7. specialized asynchronous agents only where lifecycle boundaries justify them;
8. fine-tuning only when Eval data identifies a stable, valuable error class.

The principle is simple: add complexity in response to measured failure or business need, not to satisfy a vocabulary checklist.
