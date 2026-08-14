# Security Policy

## Supported versions

This is a portfolio project, not a released product. I only maintain the latest version on the default branch.

## Repository boundary

I rewrote this public version from scratch and filled it with synthetic data. I do not put private production code, real conversations, logs, databases, screenshots, prompts, cookies, browser profiles, platform identifiers, credentials, private endpoints, or old Git history from the live system in this repository.

The secret scanner catches several common credential patterns, but it cannot prove that every file is safe. I still review staged files and Git history before pushing.

If a real secret is ever committed, deleting it in a later commit is not enough. Revoke or rotate it, remove it from published history, and check access logs and downstream copies.

## Reporting a vulnerability

Use GitHub's private vulnerability-reporting feature in the repository's **Security** tab. If private reporting has not been enabled, contact the owner through a private channel listed on the owner's GitHub profile.

Do not open a public issue containing:

- a working exploit;
- credentials, tokens, cookies, or browser-state data;
- real customer, merchant, order, or payment information;
- a private service address or production topology.

Include the affected revision, impact, and a small reproduction using synthetic data. A suggested fix is welcome. I do not promise a response time or run a bounty program for this demo.

## Security properties implemented in the demo

- strict Pydantic HTTP request and response shapes;
- structured intent plus deterministic runtime dispatch, not native Function Calling;
- current-message and stored-history redaction for demonstrated phone/email/token patterns;
- tenant/store/customer filtering in synthetic order and evidence tools;
- deterministic refund policy and explicit approval state;
- expiring exact-message claims, action deduplication, and SQLite execution idempotency;
- a no-action `refund_inquiry` route plus an explicit-action gate that derives one refund order and amount from customer text rather than trusting planner fields;
- strict `Decimal` CNY parsing that rejects ambiguous or malformed amounts;
- operator-key protection for trace, approval, and execution routes;
- Compose port publication restricted to `127.0.0.1:8000`;
- 53 unit/integration tests and a separate 50-case deterministic synthetic Eval suite.

The optional OpenAI-compatible mode receives the redacted current message, not the original text. Recent history sent to it is also sourced from redacted stored turns.

## What this demo does not cover

`X-DXL-Operator-Key` is one shared demo key. It does not establish a person's identity, role, authorization, tenant membership, or approval authority. The public message endpoint also does not authenticate its claimed tenant/customer context. Localhost-only Compose binding narrows network exposure but does not make the service production-safe.

This public demo does not include:

- signed webhooks, user authentication, operator identity, or authorization;
- distributed locks, durable queues, or multi-region ordering;
- real platform/payment connectors or authoritative timeout reconciliation;
- comprehensive DLP, encrypted storage, managed secrets, or data lifecycle controls;
- rate limiting, malware/attachment scanning, WAF, or abuse prevention;
- provider circuit breakers, usage budgets, or rollout controls;
- complete semantic response validation;
- immutable audit storage, operational monitoring, or incident-response integration;
- regulatory compliance or platform certification.

The unit/integration tests check code behavior. The synthetic Eval checks selected replies and traces. Passing 53 tests and 50/50 Eval cases shows that this demo behaves as documented; it is not a security certification or a production guarantee.

## Deployment warning

Do not connect real customer accounts, personal data, or payment permissions to this demo. Before using the design in a real system, finish and review the missing controls listed in [the safety model](docs/safety-model.md). Use approved platform interfaces and give every credential only the permissions it needs.
