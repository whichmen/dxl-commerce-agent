# Security Policy

## Supported versions

DXL Commerce Agent is an educational, portfolio-oriented reference project. Security fixes are applied only to the latest revision of the default branch; older revisions are not maintained as supported releases.

## Repository boundary

This repository is a **fresh, sanitized reference implementation**. Only newly written reference code and synthetic data belong here. Contributions must not contain production source copied from a private system, real conversations, logs, databases, screenshots, prompts, cookies, browser profiles, platform identifiers, credentials, private endpoints, or historical Git objects containing such material.

The included secret scan covers a small set of high-confidence credential patterns. It is useful defense in depth, not proof that a repository contains no secret or personal data. Review staged files and Git history before publishing.

If a live secret is ever committed, deleting it in a later commit is not sufficient. Revoke or rotate it immediately, remove it from all published history, and review access logs and downstream copies.

## Reporting a vulnerability

Use GitHub's private vulnerability-reporting feature in the repository's **Security** tab. If private reporting has not been enabled, contact the owner through a private channel listed on the owner's GitHub profile.

Do not open a public issue containing:

- a working exploit;
- credentials, tokens, cookies, or browser-state data;
- real customer, merchant, order, or payment information;
- a private service address or production topology.

Include the affected revision, impact, and minimal reproduction steps using synthetic data. A suggested mitigation is welcome. The maintainer offers no guaranteed response time or bounty program.

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
- 28 unit/integration tests and a separate 50-case deterministic synthetic Eval suite.

The optional OpenAI-compatible mode receives the redacted current message, not the original text. Recent history sent to it is also sourced from redacted stored turns.

## Deliberate limits

`X-DXL-Operator-Key` is one shared demo key. It does not establish a person's identity, role, authorization, tenant membership, or approval authority. The public message endpoint also does not authenticate its claimed tenant/customer context. Localhost-only Compose binding narrows network exposure but does not make the service production-safe.

The repository does not implement production-ready:

- signed webhooks, user authentication, operator identity, or authorization;
- distributed locks, durable queues, or multi-region ordering;
- real platform/payment connectors or authoritative timeout reconciliation;
- comprehensive DLP, encrypted storage, managed secrets, or data lifecycle controls;
- rate limiting, malware/attachment scanning, WAF, or abuse prevention;
- provider circuit breakers, usage budgets, or rollout controls;
- complete semantic response validation;
- immutable audit storage, operational monitoring, or incident-response integration;
- regulatory compliance or platform certification.

Unit/integration tests verify implementation contracts. Synthetic Eval grades selected runtime responses and traces. Passing 28 tests and 50/50 Eval cases is useful reproducible evidence for this bounded demo, not a production security guarantee or compliance result.

## Deployment warning

Do not connect real customer accounts, personal data, or financial permissions until the production-next controls in [the safety model](docs/safety-model.md) have been designed, implemented, tested, and independently reviewed. Use only platform-approved interfaces and least-privilege credentials.
