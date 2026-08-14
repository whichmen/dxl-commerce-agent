# What I Learned Building Customer-Service Automation

## Where these lessons came from

I wrote this public project from scratch with synthetic data. It lets me show the engineering ideas without publishing private production code, platform automation, credentials, prompts, customer records, or operating data.

The private system I ran supported more than ten stores. From my own operating records, customer response time was usually around 0.5–2.5 minutes and the overall labor cost fell by about half. These are rough numbers from my own operation, not independently audited figures or performance claims for this public project.

What anyone can reproduce here is narrower: 53 unit/integration tests and 50 deterministic synthetic Eval cases, all using made-up fixtures. They check this code; they do not prove the private-system numbers above.

## 1. Optimize for resolution, not agent count

A commerce customer wants a correct answer or completed action. Multiple agents discussing one bounded request do not inherently improve correctness. A single decision loop with narrow tools and a deterministic policy boundary is often easier to observe and operate.

I therefore use one planner contract and one runtime here, not a group of agents talking to each other. A larger system may split out independent jobs such as logistics monitoring or refund audits, but those jobs should communicate through clear events and permissions instead of joining every customer reply.

## 2. Separate planning from dispatch

Model-native Function Calling is useful in some systems, but it is not the only way to build an agent. In this implementation, the planner returns a structured `DecisionPlan`; trusted runtime code maps that intent onto a fixed tool path.

That distinction matters when reading the code: this project uses typed intent planning and fixed runtime dispatch, **not native function calling**. The optional OpenAI-compatible planner cannot directly name or run a tool, and the rule planner still works without a model key.

## 3. Transaction facts belong in transactional systems

Models should not remember order state, and document retrieval should not guess it. Orders, logistics, payment, inventory, and refund outcomes keep changing. My runtime reads those facts from synthetic tools scoped to one tenant, store, and customer.

RAG can help with large unstructured corpora such as manuals, policy explanations, or troubleshooting guides. It should be a read-only evidence source rather than a substitute for transaction APIs or an authority to approve a side effect. RAG is not implemented in this repository.

## 4. Policy must survive a bad planner decision

Prompts influence behavior but do not enforce authorization. Refund limits, allowed state, paid amount, evidence requirements, and approval thresholds belong in deterministic code or versioned configuration.

The runtime goes one step further for the demonstrated refund flow: it separates `refund_inquiry` from `refund_request`, requires one order ID and a closed, anchored action command explicitly present in the customer message, and re-derives the amount and policy-relevant reason rather than trusting planner fields. Amount parsing uses `Decimal`, not binary floating point. Questions, quoted examples, negations, bare amount mentions, multiple amounts, suspicious signs, and over-precise values fail closed. Tests inject unsafe planners that attempt to change the order or downgrade the reason and confirm that the runtime preserves the customer-selected order and required evidence.

## 5. Serialize by conversation, not globally

Messages in one conversation may depend on order, while unrelated conversations should not block each other. The current code uses one in-process `asyncio.Lock` for each canonical session key. It also uses an expiring SQLite claim to ensure that two processes sharing the demo database do not both handle the exact same message ID; an active duplicate fails fast, and an abandoned claim can be reclaimed after 60 seconds.

That base-runtime claim can deduplicate one exact message, but it does not order different messages or renew a long job. The synthetic channel path adds renewable fenced leases and head-of-line inbox/outbox claims for processes sharing one SQLite database. It still cannot guarantee order across machines; a real deployment needs a partitioned queue, distributed lease, or similar ownership mechanism.

## 6. At-least-once delivery demands several kinds of deduplication

The demo uses three different mechanisms:

- a canonical message key plus expiring SQLite claim prevents concurrent handling of an exact redelivery and returns the cached decision after completion;
- a canonical refund business key retains one action per scoped order;
- a caller-provided execution idempotency key returns the recorded result when replayed.

The synthetic channel path adds cursor compare-and-swap, changed-payload conflict detection, renewable fenced inbox/outbox leases, a stable delivery key, connector-binding snapshots, Browser receipt checks, and `unknown` instead of automatic replay for an expired non-idempotent send.

SQLite transaction locking and a unique index also prevent two included demo connections from completing one action twice.

The harder case is a real write that times out after the remote side may have accepted it. Before retrying, a live service has to check the backend's own operation record. This synthetic executor has no real backend, so it does not implement that step.

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

The current result is **50/50 synthetic cases passed with 0 tagged safety failures** under the deterministic rule planner. This is separate from the **53 unit/integration tests**, which directly exercise runtime, connector/worker, inbox/outbox, handoff, receipt, and other code contracts and edge conditions. Neither suite measures model cost, token use, real-service latency, customer satisfaction, or production resolution rate.

## 10. Operational metrics need definitions

Statements such as “automatically resolved” or “no failures” are not useful until their denominator, time window, workflow scope, and failure definition are explicit. A production dashboard should distinguish:

- model/tool success from customer issue resolution;
- temporary retry from unrecovered failure;
- planned escalation from silent abandonment;
- message-send success from business-action completion;
- average latency from tail latency.

I do not present my rough private-system figures as a benchmark. The only numbers anyone can reproduce here are the versioned tests and synthetic Eval cases above.

## 11. Shared secrets are not operator identity

The demo protects trace, approval, and execution routes with `X-DXL-Operator-Key`. This is useful for preventing accidental open access during a localhost demonstration, but every holder has the same authority and the supplied approver is only an audit label.

Production human approval needs authenticated operator identity, role-based authorization, tenant scope, separation of duties where required, session controls, and durable accountability. The Compose service binds to localhost to keep the demo exposure narrow; localhost binding is not itself authentication.

## 12. What came from my private system

In my private customer-service system, I used browser automation/CDP, Appium with UiAutomator2, Android `AccessibilityService`, an image/OCR bridge, a Decision API with delivery acknowledgements, and health/watchdog processes. I deployed after-sales action executors separately from message workers, so a worker that sent messages did not automatically get permission to change orders or refunds.

That paragraph describes my experience; it does not mean the private adapters are in this repository. I did not publish platform names, selectors, private addresses, device/account details, my deployment layout, or the original adapter code. The public `BrowserConnector`, `MobileConnector`, inbox/outbox, receipt, and worker code is new synthetic code written for this demo.

## 13. Frameworks are replaceable; invariants are not

LangGraph, MCP, Agent SDKs, and model function calling can improve development when their capabilities match a measured need. None automatically provides ownership validation, idempotency, policy enforcement, or auditability.

I used plain Python, FastAPI, Pydantic, and SQLite so the control flow stays easy to inspect. I would add a framework later only if the workflow, integrations, or scale made it useful.

## What works here and what I would add next

| What works in this repo | What a real deployment still needs |
|---|---|
| Structured intent plus fixed runtime dispatch | Native tool protocol only if dynamic discovery is needed |
| Scoped synthetic reads | Authenticated, least-privilege commerce APIs |
| Deterministic synthetic refund rules | Reviewed, versioned business/risk policy and regulatory controls |
| Local message/action dedupe, channel inbox/outbox, synthetic receipts, and SQLite idempotency | Durable distributed workflow plus real provider/backend reconciliation |
| In-process session serialization, an expiring HTTP message claim, and renewable fenced channel inbox/outbox leases | Partitioned durable queue, renewable distributed lease, and multi-host session ordering |
| Synthetic Browser/Mobile connectors and cautious ambiguous-delivery handoff | Platform-approved authenticated connectors and durable receipts |
| Local human generation/send fence, acknowledgement, parking, explicit unknown resolution, and guarded release | Cross-process barrier, authenticated human console, roles, SLA, and audited resume |
| Basic pre-planner and persistence redaction | Comprehensive DLP and lifecycle governance |
| Shared demo operator key | Individual identity, roles, tenant authorization, approval accountability |
| Deterministic templates and API shape validation | Semantic grounding validation if model-written responses are introduced |
| 53 tests and 50-case offline synthetic Eval | Real production metrics, adversarial sets, human review, and incident feedback |

## Practical evolution order

For a small team, I would add things in this order:

1. reliable scoped tools and deterministic business policy;
2. message/action deduplication and idempotent writes;
3. inspectable traces plus unit/integration tests and behavioral Eval;
4. real identity, authorization, privacy, observability, and deployment controls;
5. durable coordination and external-backend reconciliation;
6. optional RAG for a measured unstructured-knowledge gap;
7. specialized asynchronous agents only where lifecycle boundaries justify them;
8. fine-tuning only when Eval data identifies a stable, valuable error class.

My rule is simple: add complexity when a measured failure or business need calls for it, not just to tick off a list of fashionable terms.
