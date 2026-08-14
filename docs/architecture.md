# Architecture

## The main path

The customer-service application starts in `agent` mode unless the deployment explicitly selects `policy`. In Agent mode, OpenClaw and the configured LLM understand the request, invoke registered tools, consume their results, and write the final response. A rule planner is not the answer generator on this path.

```mermaid
flowchart TB
    subgraph Channels
        D[Douyin browser worker]
        P[Pinduoduo browser worker]
        K[Kuaishou browser worker]
        W[WeChat Channels browser worker]
        T[Taobao Appium or Android worker]
        X[Xiaohongshu Android worker]
    end

    D --> N[Normalized IncomingMessage]
    P --> N
    K --> N
    W --> N
    T --> N
    X --> N
    N --> API[Decision API /v1/decide]
    API --> PRE[Media materialization, channel capabilities, session lock, decision cache]
    PRE --> OC[OpenClaw kefu_ops Agent]
    OC --> LLM[Configured LLM]
    LLM --> PLUGIN[Native kefu-tools calls]
    PLUGIN --> TOOLAPI[Decision API /v1/tools/{tool_name}]
    TOOLAPI --> BACKEND[ERP, identity, refund, vision, and administrative backends]
    BACKEND --> PLUGIN
    PLUGIN --> LLM
    LLM --> FINAL[Final reply]
    FINAL --> SEND[Platform worker sends text/images]
    SEND --> ACK[Decision API /v1/worker/ack]
    ACK --> DB[(SQLite event, decision, and delivery state)]
```

One call from the Decision API to the `kefu_ops` Agent may contain several internal model/tool turns. “Single Agent” therefore does not mean “one model completion with no tools.” It means one clear owner for the customer turn while OpenClaw runs the tool loop inside that boundary.

## Components

### Platform workers

`workers/` contains the channel edge:

- Douyin and Kuaishou use Playwright against an authenticated Chrome CDP session.
- Pinduoduo and WeChat Channels use the authenticated browser session for incremental intake and sending.
- Taobao Qianniu has an Appium + UiAutomator2 worker and an Android AccessibilityService worker.
- Xiaohongshu Qianfan has an Android AccessibilityService worker.

Each worker extracts a stable message ID and platform nickname, stores local processing state, calls `/v1/decide`, follows the returned `send`, `skip`, or `manual` action, and acknowledges actual delivery through `/v1/worker/ack`. Platform sessions and device permissions are runtime state, not source-code constants.

### Decision API

`src/kefu_clawbot/app.py` owns the HTTP boundary. For each event it:

1. validates the Pydantic request;
2. materializes local media when applicable;
3. attaches declared channel capabilities;
4. takes an in-process lock keyed by platform, store, and platform nickname;
5. reuses a cached decision for a duplicate message ID when allowed;
6. loads up to eight recent dialogue pairs from SQLite;
7. invokes either the default OpenClaw Agent path or the explicit policy fallback;
8. stores the event and decision, then later stores the worker's delivery acknowledgement.

The local classifier adds coarse category metadata to traces. It does not decide the tool sequence and does not write the customer response in Agent mode.

### OpenClaw Agent

The `kefu_ops` workspace contains three distinct pieces:

- `AGENTS.md` contains hard behavioral constraints;
- `skills/kefu-core/SKILL.md` contains the order, logistics, refund, evidence, and reply workflow;
- `plugins/kefu-tools/` publishes business functions to OpenClaw.

The model receives the current message, recent history, store/channel context, and image attachment when available. It selects tools through OpenClaw's native tool interface. After tool results come back, the model writes plain customer-facing text and optional `[[IMAGE_URL:...]]` markers. The Decision API validates that a non-empty reply was produced, removes image markers from the text, and only returns images when the channel declares image-send support.

### Business tool backend

The OpenClaw plugin is a narrow proxy. It normalizes arguments, rejects extra schema fields, removes model-supplied connection and credential fields, adds trusted event context, and calls the authenticated `/v1/tools/{tool_name}` endpoint.

The Python backend connects to:

- the configured ERP/order system for order, logistics, after-sales, and refund facts;
- the local identity service for platform-nickname mappings and blacklist state;
- the configured vision provider for the optional compatibility image tool;
- bounded local administrative functions when their explicit gates and allowlists are enabled.

The model never supplies the backend URL, API key, password, headers, or deployment-owned special-order marker.

### Persistence and operational APIs

The main service stores events, decisions, media references, and delivery receipts in SQLite. It exposes health, recent-session, trace, summary, and worker-ack endpoints. The optional identity service stores nickname mappings and blacklist rows in its own SQLite database.

This is local process coordination, not a distributed queue. Multi-host deployment needs an external durable queue or another mechanism for cross-host session ownership, plus managed storage and centralized observability.

## Tool registration model

There are 13 plugin tools. Four read-oriented tools are registered by default:

- `order_lookup`
- `logistics_lookup`
- `refund_history_lookup`
- `refund_case_lookup`

The other nine tools remain in source but require explicit environment flags. File reads and shell commands also require real-path roots or command allowlists at the backend. See [`../openclaw/plugins/kefu-tools/README.md`](../openclaw/plugins/kefu-tools/README.md) for the exact mapping.

The setup script allows only `kefu-tools` for `kefu_ops` by default. General OpenClaw built-ins are available only when `CLAWBOT_ENABLE_OPENCLAW_BUILTINS=1` was set before setup.

Because the plugin runs inside the OpenClaw Gateway process, its `CLAWBOT_TOOL_*` values and tool-registration flags must be visible to that process. [`../scripts/sync_openclaw_gateway_env.sh`](../scripts/sync_openclaw_gateway_env.sh) copies only the allowed key names from the repository `.env` to `~/.openclaw/.env` without printing their values.

## Agent mode and policy mode

| Mode | Selected by | What produces the answer | Intended use |
|---|---|---|---|
| Agent | `CLAWBOT_DECISION_MODE=agent` | OpenClaw + configured LLM, with native tool calls | Normal customer-service path |
| Policy | `CLAWBOT_DECISION_MODE=policy` | `PolicyEngine` categories and fixed templates | Explicit fallback and isolated smoke checks |

`scripts/run_dev.sh` defaults to Agent mode and fails early if the OpenClaw executable is missing. `docker-compose.yml` explicitly selects Policy mode because the image does not bundle an OpenClaw account or Gateway. A passing Compose smoke check therefore does not validate model reasoning or the tool loop.

## Memory and RAG

The current Agent path has short-term dialogue memory in SQLite plus OpenClaw session continuity. Transaction truth remains in tools: order, shipment, inventory, and refund data change too often to come from model memory.

RAG is not part of the current transaction path. It can be added for large read-only document sets such as product manuals, after-sales policies, and platform instructions. Retrieved text should remain evidence, not authority to perform a refund or change an order.

## Failure behavior

- Duplicate message IDs can reuse the stored decision instead of calling the model again.
- Same-session calls are serialized inside one Decision API process.
- A model call that fails or returns JSON/internal output is rejected instead of being sent as a customer reply.
- `DecisionClient` can return a configured short fallback on timeout or network failure.
- Workers store local state and report health; the watchdog can restart crashed or stale workers and hold when manual login/action is required.
- Delivery is recorded only after the worker calls `/v1/worker/ack`.

These mechanisms make the single-host system practical, but they are not a claim of distributed exactly-once delivery. Cross-host ordering, external-write reconciliation, role-based operator identity, and centralized audit retention belong in the deployment layer.

## Verification boundary

The current validation runs 72 unit/integration tests and 50 deterministic offline Eval cases. Tests cover the API, OpenClaw invocation boundary, tool gates, identity service, storage, and the separate deterministic reliability components. CI substitutes the OpenClaw/model boundary; a live provider and Gateway are required to validate real model output.
