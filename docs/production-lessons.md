# What I Learned Building Ecommerce Customer-Service Agents

I built this system around one practical goal: handle real customer-service work reliably. The model matters, but the surrounding engineering decides whether a useful answer reaches the correct customer and whether a business action is safe.

## 1. The model must be in the real path

My default path uses OpenClaw and an LLM to understand the request, decide whether a tool is needed, read tool results, and write the final reply. A keyword classifier can help with statistics or fallback behavior, but it cannot replace language understanding for the variety of messages customers actually send.

The code therefore makes the distinction explicit:

- `agent` mode is the normal path: model + Skill + native tools + final model response;
- `policy` mode is a fixed-rule fallback and container smoke path.

Calling both of these “Planner” hides an important difference. A fixed planner can route a few known phrases; it cannot produce the same answer quality as a configured model with current business facts.

## 2. One Agent can still have a real tool loop

I do not need several agents just to call several tools. One `kefu_ops` Agent can run multiple internal turns:

```text
understand request -> call order tool -> inspect result -> call logistics tool -> write reply
```

That is already an Agent loop. I would split another Agent out only when it has a genuinely separate lifecycle, permission set, queue, or owner—for example, proactive refund audit or long-running logistics monitoring.

## 3. Rules belong in Skills; authority belongs in code

`kefu-core/SKILL.md` tells the model how I want customer-service cases handled: what to verify, which tool to call, what not to promise, and when to ask for more evidence. This makes behavior readable and easy to improve.

Prompts and Skills are not authorization. Refund limits, tool registration, API authentication, filesystem roots, command allowlists, and merchant-owned markers still belong in trusted configuration and backend code.

## 4. Live facts should come from tools

Orders, shipment events, paid amounts, refund status, and inventory keep changing. The model should not remember or invent them. It should query the ERP or another authoritative system during the customer turn.

RAG is useful for a different problem: searching large, mostly textual material such as product manuals, after-sales explanations, and platform policies. I would add RAG when that document volume creates a measured gap. It would remain read-only evidence, not a substitute for transaction APIs or permission checks.

## 5. Platform automation is part of the product

A good reply is useless if the system cannot reliably receive and send messages. That is why this repository includes browser workers, an Appium worker, Android AccessibilityService projects, local state, acknowledgements, health reporting, watchdog scripts, and systemd units.

Different platforms require different mechanics:

- browser customer-service consoles work well with dedicated Chrome profiles, CDP, and Playwright;
- mobile apps may need Appium/UiAutomator2 or a purpose-built AccessibilityService;
- an API-based page flow and a DOM-based page flow fail in different ways;
- login expiry, popups, page upgrades, and device disconnects need explicit health states instead of silent retries.

I keep the normalized Decision API contract stable so platform-specific changes do not require rewriting the Agent.

## 6. “Model answered” is not “customer received it”

I track three separate stages:

1. the Decision API obtained an Agent result;
2. the worker attempted the platform send;
3. the worker acknowledged what was actually sent.

The `/v1/worker/ack` path and delivery table make this distinction visible. It is much easier to diagnose a slow model, broken selector, expired login, or failed send when those stages are not collapsed into one success counter.

## 7. Conversation identity and ordering are easy to get wrong

The session key includes the platform, store, and platform nickname. Same-session requests take an in-process lock, while duplicate message IDs can reuse the stored decision. Workers also keep platform-local state for their own intake and send loops.

This is appropriate for the current single-host layout. It is not a distributed exactly-once claim. A multi-host deployment needs durable partitioning, cross-host ownership, and authoritative reconciliation for writes whose remote outcome is uncertain.

## 8. Images need evidence discipline

The main image path sends the original attachment to the model. If that path is unsupported, the bridge can create an isolated image summary and retry the main conversation with the summary.

The important rule is simple: unreadable content stays unreadable. The model should ask for a clearer screenshot instead of guessing an order ID, clothing code, damage state, or same-item result. Any ID read from an image still needs verification through an order tool before it becomes a customer-facing fact.

## 9. Powerful tools should be absent until enabled

The Agent receives four normal read tools by default. Refund approval, blacklist changes, batch jobs, file reads, and shell execution are not merely discouraged in a prompt; they are not registered until a deployment flag enables them.

The backend checks the same high-risk categories again. File and shell tools also require explicit allowlists. This keeps the normal customer Agent useful without giving every conversation the full power of the host machine.

## 10. Model and Gateway environment are separate processes

The Decision service reads the repository `.env`. The OpenClaw plugin runs in the Gateway process and therefore needs its own environment. Missing this distinction produces a confusing failure: the Agent can see a tool name but cannot authenticate to the tool backend.

I added `scripts/sync_openclaw_gateway_env.sh` to copy only the 14 plugin-related `CLAWBOT_*` names into `~/.openclaw/.env` without printing values or replacing unrelated OpenClaw settings. The Gateway must be restarted after a change.

## 11. Identity mapping deserves its own small service

The nickname visible on one platform does not always match the identity used by the order system. I separated that mapping into `kefu_identity_service`, with its own database, API key, lookup endpoint, import script, and blacklist compatibility endpoints.

That avoids teaching the model a private mapping and makes updates auditable. Ambiguous or missing mappings can be surfaced as data problems instead of guessed by the LLM.

## 12. Eval should check behavior, not only fluent prose

A polished sentence can still use the wrong order, skip a required tool, or cross a permission boundary. The repository keeps deterministic offline cases alongside unit/integration tests. The current validation reports 72 passing tests and 50/50 passing offline Eval cases with no tagged safety failures.

CI mocks the live OpenClaw/model boundary because it has no provider account. I still need a live smoke check after configuring a provider, because mocked boundary tests cannot measure answer quality, tool-choice quality, latency, cost, or provider-specific image handling.

## 13. Operational numbers need a defined source

My actual customer-service operation covered Taobao, Pinduoduo, Douyin Ecommerce, Kuaishou Ecommerce, Xiaohongshu, and WeChat Channels across browser and mobile workflows. In my operating records, routine pre-sales and after-sales work was handled automatically, and customer response time was usually about 0.5–2.5 minutes.

Those numbers describe my operation. Repository test counts and offline Eval results describe this code. Keeping the two sources separate makes both statements more useful.

## The order I would extend the system

For a small team, I would usually invest in this order:

1. keep platform workers and business tools stable;
2. add real delivery and external-write reconciliation;
3. add per-user operator identity, roles, and a human handoff console;
4. add privacy controls, monitoring, cost/latency dashboards, and staged rollout;
5. expand Eval with sampled failures from actual traffic;
6. add RAG when document lookup is a real bottleneck;
7. add specialized Agents only when their workflow and permissions are truly separate;
8. fine-tune a smaller model only after Eval shows a stable, high-volume error class worth training away.

The framework name is less important than the result: correct facts, safe actions, reliable delivery, and a clear path for failures.
