# Security Policy

## Supported version

I maintain the latest version on the default branch.

## Reporting a vulnerability

Use GitHub private vulnerability reporting in the repository's **Security** tab. If it is unavailable, contact me through the private channel on my GitHub profile.

Do not put a working exploit, credential, Cookie, browser profile, customer/merchant record, private service address, or production topology in a public issue. Include the affected revision, impact, and the smallest safe reproduction you can provide.

## Security design

The normal path uses OpenClaw and an LLM with native tool calling. The model is not the permission boundary. Access is limited in layers:

- `kefu_ops` receives only `kefu-tools` by default;
- four read-oriented tools are registered by default, while nine administrative/high-risk tools require explicit flags;
- tool schemas reject unknown fields and strip model-supplied connection/credential fields;
- Decision, tool, identity, and optional WeCom hub paths use separate configurable keys;
- refund, blacklist, offline, file, and shell capabilities are checked again in the Python backend;
- file and shell tools require real-path roots or command allowlists after their tool flags are enabled;
- the deployment-specific special-order marker is injected from environment and cannot be overridden by model arguments;
- secrets, platform sessions, browser profiles, identity mappings, databases, media, and logs are ignored or supplied at runtime rather than hard-coded.

See [the safety model](docs/safety-model.md) for the exact tool flags and trust boundaries.

## Required deployment settings

Before connecting real accounts:

1. replace all example keys with different random values;
2. keep the Decision API, tool API, identity service, CDP ports, and ADB on trusted interfaces/networks;
3. run `bash scripts/sync_openclaw_gateway_env.sh` so the Gateway receives only the plugin-related environment names, then restart it;
4. leave unused tools unregistered and configure the smallest practical allowlists for enabled tools;
5. protect `.env`, SQLite databases, media, CSV mappings, Chrome profiles, Android settings, and logs with appropriate OS permissions;
6. review the selected model/provider's handling of customer data and implement the required minimization/redaction, retention, deletion, encryption, backup, and audit controls;
7. stage and monitor any tool that changes refunds, orders, blacklist state, files, or commands.

An empty configured API-key value disables that service's shared-key check. Shared keys authenticate another process; they are not a replacement for individual operator login, roles, tenant authorization, or approval accountability.

## Verification boundary

The current automated validation includes 72 unit/integration tests, 50 deterministic offline Eval cases, a high-confidence secret scan, syntax checks, wheel packaging, and a container build. Tests cover the OpenClaw invocation boundary, tool gates, file/shell allowlists, identity service, and existing reliability code.

CI uses a controlled substitute for the OpenClaw/model boundary and has no live platform login, ERP credential, model account, or Android device. A passing CI run is not a security certification and does not validate a newly configured provider, platform UI, business rule, or production data-handling policy.

If a secret is committed, removing it in a later commit is not enough. Revoke or rotate it, remove it from published history, and review access logs and downstream copies.
