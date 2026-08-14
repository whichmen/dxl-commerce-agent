# DXL Commerce Agent

A privacy-safe, production-minded commerce-support agent reference implementation.

[![CI](https://github.com/whichmen/dxl-commerce-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/whichmen/dxl-commerce-agent/actions/workflows/ci.yml)

> [!IMPORTANT]
> This is a **fresh, sanitized reference implementation** built for architecture review and portfolio demonstration. It is not a source dump, mirror, or historical snapshot of the author's private production system. Every customer, store, order, policy, conversation, credential placeholder, and evaluation case in this repository is synthetic.

[中文说明](README_CN.md) · [Architecture](docs/architecture.md) · [Safety model](docs/safety-model.md) · [Production lessons](docs/production-lessons.md) · [Security](SECURITY.md)

## What is implemented

The repository demonstrates the engineering around a model, rather than presenting a chatbot-only demo:

- a FastAPI service with strict request and response models;
- a deterministic rule planner that runs without a model key;
- an optional OpenAI-compatible planner that returns a closed, structured intent;
- deterministic runtime dispatch from that intent to scoped commerce tools;
- synthetic order, logistics, product, and evidence lookups;
- deterministic refund policy, explicit approval, and sandbox-only execution;
- a fail-closed, anchored command grammar before free text may create a sandbox refund proposal;
- reclaimable SQLite message leases, business-action deduplication, and execution idempotency;
- per-session serialization within one process;
- SQLite-backed dialogue history, redacted traces, actions, and selected audit events;
- 28 unit/integration tests and 50 deterministic synthetic Eval cases.

This implementation does **not** use native model function calling. The planner emits a typed intent such as `logistics_status` or `refund_request`; trusted runtime code then chooses the allowed tools and derives the refund order, amount, and policy-relevant reason from the customer message. It also does not require LangGraph, MCP, RAG, or multi-agent orchestration.

## Architecture at a glance

```mermaid
flowchart LR
    U[Customer request] --> V[Pydantic validation]
    V --> S[In-process session lock]
    S --> D[SQLite message claim / deduplicate]
    D --> X[Redact current message]
    X --> P[Rule planner]
    X -. optional .-> L[OpenAI-compatible planner]
    P --> I[Structured intent]
    L --> I
    I --> R[Deterministic runtime dispatch]
    R --> Q[Scoped read tools]
    R --> G[Refund policy gate]
    G --> A[Approval and sandbox action store]
    Q --> O[Deterministic response template]
    A --> O
    R --> T[(Redacted trace / audit data)]
    E[50-case synthetic Eval] -. replays .-> R
```

The current customer message is redacted before either planner sees it, including in OpenAI-compatible mode. Stored recent history is redacted as well. A refund write path requires one explicit order ID, an anchored action phrase, and a strict amount (or explicit full-refund phrase); questions, quoted examples, negations, bare amount mentions, and malformed signed amounts fail closed. Transaction facts come from scoped synthetic tools, while policy code—not model prose—decides whether a refund proposal is denied, automatically approved, or held for operator approval.

See [Architecture](docs/architecture.md) for the exact request lifecycle and implementation boundaries.

## Why tools instead of RAG for orders

Orders, logistics, refund status, prices, and inventory are mutable structured facts. They should come from an authoritative API or database through scoped tools. RAG can be useful for large unstructured corpora such as manuals or platform policies, but it should not replace a transactional system. RAG is therefore a possible production extension, not part of this repository's required path.

## Quick start

The default demo is deterministic, uses only synthetic fixtures, and needs no model API key.

### Option A: Docker Compose

```bash
docker compose up --build
```

Compose publishes the service only on `127.0.0.1:8000`; it is not exposed on every host interface. By default, Compose supplies the demo operator key `local-synthetic-demo-key`.

### 60-second curl walkthrough

This sends a synthetic logistics question and then reads its redacted trace. `jq` is used only to display the JSON and extract the trace ID.

```bash
BASE_URL=http://127.0.0.1:8000
OPERATOR_KEY=local-synthetic-demo-key

RESPONSE="$(curl -sS -X POST "$BASE_URL/v1/messages" \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id": "demo",
    "channel": "sandbox",
    "store_id": "STORE-001",
    "customer_id": "CUST-0042",
    "message_id": "readme-logistics-001",
    "text": "SYN-ORDER-1001 的物流到哪里了？"
  }')"

printf '%s\n' "$RESPONSE" | jq .
TRACE_ID="$(printf '%s\n' "$RESPONSE" | jq -r .trace_id)"

curl -sS "$BASE_URL/v1/traces/$TRACE_ID" \
  -H "X-DXL-Operator-Key: $OPERATOR_KEY" | jq .
```

If you set `DXL_OPERATOR_KEY` in a repository-root `.env`, use that same value in the curl header. Docker Compose reads `.env` for variable interpolation. The Python application itself does **not** load `.env` files automatically.

`X-DXL-Operator-Key` is only a shared demo guard for trace, approval, and execution endpoints. It is not production identity authentication, user authorization, tenant authentication, or a replacement for signed webhooks/OIDC.

### Option B: local editable install

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
export DXL_OPERATOR_KEY=local-synthetic-demo-key
dxl-commerce-agent
```

The editable install runs on `127.0.0.1:8000`. Export settings in the shell before starting it; copying `.env.example` to `.env` is not enough for this local Python command because no dotenv loader is used.

The interactive API documentation is available at `http://127.0.0.1:8000/docs`.

| Method and path | Purpose | Access in this demo |
|---|---|---|
| `GET /health` | Liveness and planner mode | Open |
| `POST /v1/messages` | Process a synthetic customer message | Open demo ingress |
| `GET /v1/traces/{trace_id}` | Read a redacted trajectory | Demo operator key |
| `POST /v1/actions/{action_id}/approve` | Approve a gated sandbox refund | Demo operator key |
| `POST /v1/actions/{action_id}/execute` | Execute an approved sandbox refund with `Idempotency-Key` | Demo operator key |

## Verification

Run verification from the editable development environment:

```bash
python -m pytest
python -m dxl_agent.eval_runner
ruff check .
ruff format --check .
python scripts/secret_scan.py
```

Verified repository snapshot:

| Check | Result | What it establishes |
|---|---:|---|
| Unit/integration tests | **28 passed** | Deterministic code behavior across runtime, API, SQLite concurrency, policy, redaction, scoping, and idempotency |
| Synthetic offline Eval | **50/50 passed; 0 safety failures** | Expected intent, tool trajectory, grounding fields, policy outcomes, deduplication, isolation, malformed-input handling, and redaction on versioned synthetic scenarios |

Tests and Eval are deliberately different. Tests exercise implementation contracts and edge cases. Eval replays behavioral scenarios and grades structured responses and traces. Neither result is a production benchmark, a model-quality benchmark, or proof of compliance.

## Implemented now vs production-next

| Implemented in this repository | Needed before real deployment |
|---|---|
| Synthetic fixtures and sandbox-only refunds | Platform-approved, authenticated business connectors |
| Pydantic API validation and scoped lookup arguments | Real identity, authorization, webhook verification, and rigorous tenant boundary enforcement |
| In-process session locks plus fail-fast, reclaimable SQLite message claims | Durable partitioned queues and renewable distributed leases for cross-process or multi-region ordering |
| SQLite dedupe, actions, execution idempotency, traces, and selected audit events | Managed transactional storage, retention/access controls, encryption, and backend reconciliation after ambiguous writes |
| Basic phone/email/token redaction before planning and persistence | Reviewed DLP, data minimization, provider governance, and deletion workflows |
| Closed structured-intent parser with deterministic fallback | Provider timeouts, bounded retries, circuit breaking, budgets, rollout controls, and broader response validation |
| Synthetic offline Eval and CI tests | Governed production telemetry, sampled human review, adversarial testing, and incident response |

## Repository boundaries

Included:

- newly written reference code and synthetic fixtures;
- deterministic planning, dispatch, policy, approval, and sandbox execution;
- tests, offline Eval, CI, container configuration, and design documentation.

Not included:

- real platform automation or undocumented platform interfaces;
- production source, prompts, cookies, browser profiles, databases, logs, or screenshots;
- customer PII, merchant identifiers, private endpoints, or credentials;
- proprietary operational rules or private deployment topology;
- any claim that synthetic metrics equal production performance.

## Production background disclosure

This design is informed by lessons from a privately operated automation system. The author reports, in aggregate, that the private system supported more than ten stores, six concurrent live rooms, more than twenty hours of generated media per day, and a fleet of more than ten GPU-equipped machines; reported customer-response times were roughly 0.5–2.5 minutes and labor cost was reduced by about half. These figures are **self-reported, approximate, not independently audited, and not benchmarks for this repository**. No production data or source code was copied here.

## Intended use

Use this repository for learning, interviews, architecture review, and portfolio demonstration. Do not connect it to real accounts, customer data, or financial authority without implementing and reviewing the production-next controls above.

## Contact

For role or project discussions, contact Wei Zhang on WeChat: `whichmen`.

## License

Released under the [MIT License](LICENSE). Third-party services and commerce platforms have their own terms; this license does not grant permission to bypass them.
