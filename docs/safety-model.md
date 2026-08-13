# Safety Model

## Scope

This document describes the implemented safeguards and explicit limits of the public reference repository. The project is a **fresh, sanitized implementation** with synthetic fixtures and sandbox-only actions. It is a portfolio artifact, not a security certification or production-ready customer-service system.

## Safety objective

The current design aims to keep policy and side-effect decisions deterministic even when a planner is wrong, a message is delivered twice, or customer text contains instruction-like content. Model output is treated as a structured intent suggestion. Trusted runtime code selects the execution path, supplies scoped context, evaluates refund policy, and controls action state.

The project does not use native model function calling. There is therefore no model-generated tool invocation to execute. The optional OpenAI-compatible provider only supplies intent/entity fields, while customer replies remain deterministic templates.

## Trust boundaries

Untrusted inputs include:

- customer text and supplied synthetic evidence identifiers;
- output from either planner;
- tenant, channel, store, customer, message, and approver labels received by this demo API;
- environment variables and local configuration.

Trusted code in this bounded implementation includes:

- Pydantic request/response models;
- canonical scoped-key construction;
- runtime intent dispatch and trusted-context attachment;
- fixture ownership filtering;
- deterministic refund policy;
- SQLite state transitions and idempotency checks;
- basic redaction and trace construction.

The public `/v1/messages` endpoint does not authenticate the claimed tenant/customer context. Scoping prevents one supplied context from reading a different fixture record, but it does not prove who the caller is. Real identity and authenticated context construction are production-next requirements.

## Current controls

| Risk | Implemented control | Important limit |
|---|---|---|
| Planner proposes an unsafe path | Planner returns only a `DecisionPlan`; runtime owns deterministic dispatch and policy | This is not a general formal verifier |
| Simple prompt-injection phrase | Rule planner recognizes a bounded marker set and returns no tool path | Marker detection is intentionally incomplete; authorization must not depend on it |
| Cross-customer/store fixture lookup | Order/product/evidence tools filter trusted runtime arguments by available scope | API caller identity is not authenticated in this demo |
| Hallucinated transaction details | Replies use synthetic tool results and named `facts_used` fields | There is no standalone semantic response validator |
| Unsafe refund | Code checks existing refund, amount, paid amount, maximum, evidence, and approval threshold | Rules are small synthetic examples, not legal/business policy |
| Duplicate message/action | Expiring SQLite claim plus cached response for an exact message ID; one refund business key per scoped order | Different messages in one session are not ordered across processes or hosts |
| Duplicate execution | SQLite transaction, action state, idempotency record, and unique action index | There is no external backend to reconcile |
| Same-session race | One `asyncio.Lock` per session | Locking is single-process only |
| Basic PII persistence/provider exposure | Phone, email, and token-like text are redacted before persistence and planning | This is not comprehensive DLP |
| Sensitive operator routes | Shared `X-DXL-Operator-Key` required for trace/approve/execute | Shared key is not identity authentication or authorization |
| Malformed HTTP request | Strict Pydantic request types, lengths, patterns, and forbidden extras | No rate limiter, WAF, malware scan, or signed webhook |

## Planner data handling

Before invoking either planner, the runtime applies its basic redactor to the **current customer message**. This also applies in OpenAI-compatible mode. Recent history comes from redacted persisted turns, and the model adapter sends at most four recent entries truncated to 500 characters each plus a current message truncated to 2,000 characters.

The demonstrated redactor covers Chinese mobile-number patterns, email patterns, and a limited set of bearer/API-key-like strings. It does not claim to cover names, addresses, payment data, international telephone formats, images, arbitrary identifiers, or all secret encodings.

## Actual side-effect protocol

The implemented sandbox refund flow is:

1. Require a full match against a closed action-command grammar and exactly one synthetic order ID explicitly present in the current customer message. Questions, quoted examples, negations, bare amount mentions, and suspicious signed amounts fail closed.
2. Resolve that customer-selected order through the tenant/store/customer-scoped lookup, then derive refund amount and policy-relevant reason from the current message.
3. Resolve evidence through matching tenant/store/customer/order scope when required.
4. Evaluate deterministic amount, state, evidence, and approval rules.
5. Deny without creating an action, or create/reuse one scoped refund action.
6. Require the demo operator key on approval and execution API calls.
7. Require an approved state plus caller-generated `Idempotency-Key` to execute.
8. Atomically record one synthetic result and change the action to `succeeded`.
9. Return the recorded result when the original idempotency key is replayed; reject a different key after success.

The model cannot approve or execute its own proposal. The executor never contacts a financial or commerce platform.

An ambiguous external write, timeout reconciliation, and authoritative backend operation lookup are **not implemented** because there is no external write backend. They are mandatory production-next design work before real side effects are enabled.

## Prompt-injection stance

String detection is a monitoring and demo behavior, not the primary authorization control. Even if the optional planner returns a mistaken intent, it cannot name or call an arbitrary tool. Trusted runtime code chooses among fixed branches, scopes lookup arguments, re-derives policy-relevant refund facts, and runs deterministic policy before creating a sandbox action.

There is no RAG or external free-text tool result in the current implementation. If either is added, returned text must remain data rather than instructions and must not expand runtime authority.

## Trace and audit handling

Traces contain a hashed public session identifier, status, structured steps, selected public tool arguments, and named grounding fields. Stored dialogue uses the basic redactor. Selected high-level action and trace events are also written to a local SQLite audit table.

The current trace/audit implementation is not tamper-evident, append-only storage with independent custody, and it has no retention schedule or role-based access. The shared operator key only prevents unauthenticated reads when it is configured; it does not identify an individual operator.

## Actual failure behavior

| Condition | Current behavior |
|---|---|
| OpenAI-compatible transport or parse error | Falls back to the deterministic rule planner |
| Exact duplicate already in flight | Fails fast with HTTP 409 and `Retry-After: 1`; an abandoned 60-second claim can later be reclaimed |
| Unknown or unsupported intent | Returns a clarification or out-of-scope template without tools |
| Scoped order/product not found | Returns a non-invented not-found/clarification response |
| Refund-policy question or missing amount | Explains the demo rules or asks for a precise amount; no action is created |
| Negative, over-precise, or excessively long refund amount | Fails closed with clarification; no action is created |
| Missing required verified evidence | Policy denies; no action is created |
| Refund above paid amount or policy maximum | Policy denies; no action is created |
| Amount above auto-approval threshold | Stores `pending_approval`; execute is rejected until approval |
| Missing/wrong demo operator key | Operator endpoint returns 401; no configured key returns 503 |
| Execution before approval | Returns conflict through the API |
| Original execution idempotency key replayed | Returns the stored synthetic result with `deduplicated=true` |
| Different execution key used after success | Returns conflict rather than executing again |

Generic tool retries, external-write timeout handling, a circuit breaker, token/cost budgets, rate limiting, human takeover state, and audit-sink fail-closed behavior are not implemented.

## Verification boundary

At the documented repository snapshot:

- **28 unit/integration tests** exercise code contracts across runtime, policy, API protection, redaction, SQLite concurrency, scoping, and idempotency.
- **50/50 deterministic synthetic Eval cases pass with 0 tagged safety failures**. Eval replays versioned customer scenarios and checks selected intent, status, required/forbidden tool names, policy fields, grounding-field names, trace steps, malformed refund inputs, deduplication, and absence of specified sensitive fragments.

Unit/integration tests and Eval are different evidence. Tests directly exercise implementation behavior and edge cases; Eval grades structured runtime trajectories for synthetic scenarios. Passing either does not establish production security, model robustness, compliance, latency, cost, or business outcomes.

## Production-next controls

Before using real accounts, data, or financial authority, add and independently review:

- authenticated and signed channel ingress;
- user/operator identity, authorization, roles, and tenant-bound trusted context;
- platform-approved least-privilege connectors;
- durable partitioned queues and renewable leases for cross-process ordering;
- managed transactional storage and authoritative backend reconciliation;
- comprehensive DLP, encryption, secrets management, retention, and deletion;
- bounded provider timeouts/retries, circuit breaking, usage budgets, and rollout controls;
- semantic response grounding checks if model-written responses are introduced;
- rate limiting, abuse controls, attachment scanning, and availability protections;
- immutable or independently protected audit telemetry, monitoring, and incident response;
- legal, privacy, regulatory, and platform-policy review.

## Reporting vulnerabilities

Follow [SECURITY.md](../SECURITY.md). Do not place customer data, live credentials, private system details, or exploit material in a public issue.
