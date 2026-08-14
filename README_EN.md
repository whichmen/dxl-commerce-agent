# DXL Commerce Agent

This is the complete ecommerce customer-service automation system I built. Its default path is not a rule planner that assembles canned replies: **OpenClaw and an LLM** understand each customer request, use conversation context, select the required tools, read current business data, and write the final response.

The repository brings platform intake, browser and Android automation, the Agent runtime, order and logistics tools, after-sales controls, conversation memory, delivery acknowledgements, recovery, monitoring, and Eval into one codebase.

[![CI](https://github.com/whichmen/dxl-commerce-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/whichmen/dxl-commerce-agent/actions/workflows/ci.yml)

[中文说明](README.md) · [Architecture](docs/architecture.md) · [Customer-service flow](docs/customer-service-architecture.md) · [Safety boundaries](docs/safety-model.md) · [Engineering lessons](docs/production-lessons.md) · [Security](SECURITY.md)

## What it does

- handles pre-sales, order, logistics, shipment, quality, wrong-item, missing-item, refund, and other after-sales conversations;
- normalizes browser, Appium, and Android AccessibilityService messages from six commerce platforms;
- lets the model follow the `kefu-core` business skill and recent conversation context to decide when to query orders, logistics, refund history, or after-sales evidence;
- exposes current business facts through native OpenClaw tool calling instead of asking the model to guess from memory;
- serializes each conversation, deduplicates messages, caches decisions, and records delivery acknowledgements;
- supports text and image messages, safe fallback, worker `manual` handoff state, health checks, and watchdog recovery;
- persists conversations, decisions, related state, and delivery results in SQLite, with operational query endpoints;
- continuously checks the code with automated tests, offline Eval, secret scanning, and CI.

## Technology stack

The items below are backed by code and executable paths in this repository rather than planned keywords. Capabilities that require an additional service or permission are marked optional.

| Area | Technologies and engineering capabilities used |
|---|---|
| LLM and Agent | OpenClaw, LLM, Agent loop, native function/tool calling, Skills, prompts, and business rules |
| Agent memory | Recent dialogue in SQLite, OpenClaw sessions, and conversation context scoped by platform/store/customer |
| Agent tool system | TypeScript OpenClaw plugin, JSON Schema, 13 business tools, and an authenticated HTTP Tool Gateway |
| Backend services | Python 3.11/3.12, FastAPI, Uvicorn, Pydantic v2, and HTTP APIs |
| Concurrency and state | asyncio, per-conversation serialization, message deduplication, decision caching, idempotency keys, and delivery acknowledgements |
| Data storage | SQLite for dialogue, decisions, identity mappings, worker state, and delivery results |
| Browser automation | Chrome CDP, Playwright Async API, isolated browser profiles, and platform-page message intake/delivery |
| Mobile automation | Appium, UiAutomator2, ADB, and Android reverse-port forwarding |
| Native Android | Kotlin, AccessibilityService, OkHttp, Kotlin Coroutines, and local configuration UI |
| Multimodal | Native OpenClaw image input; optional Qwen-VL, Hermes OCR, screenshot cropping, and OCR |
| Commerce systems | ERP integration, orders, logistics, after-sales, refunds, and customer-identity mapping |
| Enterprise WeChat (optional) | Callback signature verification, AES encryption/decryption, multi-application Hub, and text/image delivery |
| Reliability | Worker watchdog, timeout/failure recovery, health checks, the `manual` handoff protocol, and systemd |
| Testing and Eval | pytest, pytest-asyncio, HTTPX/ASGI integration tests, 72 automated tests, and 50 deterministic offline Eval cases |
| Engineering | Ruff, Mypy, secret scanning, Python wheels, Docker Compose, and GitHub Actions |
| Security controls | Separate API keys, tool-registration gates, model-argument filtering, file/command allowlists, and backend refund validation |

## Supported platforms

The repository includes the platform worker source, not only connector interfaces.

| Platform | Integration | Main code |
|---|---|---|
| Taobao | Qianniu app through Appium + UiAutomator2; a separate Android `AccessibilityService` worker is also included | `workers/qianniu_mobile_worker.py`, `workers/android/qianniu-accessibility-worker/` |
| Pinduoduo | Authenticated Chrome session through CDP/Playwright, with incremental message intake and sending | `workers/pdd_auto_reply.py` |
| Douyin Ecommerce | Feige customer-service console through Chrome CDP + Playwright | `workers/douyin_auto_reply.py` |
| Kuaishou Ecommerce | Kuaishou Shop customer-service console through Chrome CDP + Playwright | `workers/kuaishou_auto_reply.py` |
| Xiaohongshu | Qianfan app through an Android `AccessibilityService` | `workers/android/qianfan-accessibility-worker/` |
| WeChat Channels | WeChat Channels Shop customer-service console through an authenticated Chrome session and CDP/Playwright | `workers/weixin_auto_reply.py` |

Platform sessions, device permissions, and store configuration stay on the deployment machine. When a platform changes its UI, selectors or page-call logic may need to be updated. See [`workers/README.md`](workers/README.md) for platform-specific commands.

## What the model actually does

The default `CLAWBOT_DECISION_MODE=agent` follows this path:

```mermaid
flowchart LR
    A[Six platform workers] --> B[Normalized IncomingMessage]
    B --> C[Decision API /v1/decide]
    C --> D[Session lock, history, media, channel capabilities]
    D --> E[OpenClaw kefu_ops Agent]
    E --> F[LLM understands and plans]
    F --> G[Native kefu-tools calls]
    G --> H[Order, logistics, refund, identity, and vision backends]
    H --> F
    F --> I[Final text or image response]
    I --> J[Worker sends to platform]
    J --> K[/v1/worker/ack records delivery]
```

The LLM is a working part of the system:

1. It reads the current message, recent dialogue, platform, store, and channel context.
2. It follows `openclaw/workspace-kefu-ops/skills/kefu-core/SKILL.md` to choose the processing order.
3. It uses native OpenClaw function/tool calling whenever it needs a business fact.
4. Tool results return order, logistics, refund, identity, or image facts to the model.
5. The model writes the customer-facing response, and the worker sends it back to the platform.

`classifier.py` only supplies coarse classification metadata; it does not write the answer. `CLAWBOT_DECISION_MODE=policy` is an explicit fixed-rule fallback for environments without a model, not the default business path.

The main transaction path uses one Agent. A single OpenClaw Agent invocation can contain several model/tool turns before the final reply, so native tool use does not require splitting every request across multiple agents.

## Thirteen Agent tools

All tools live in `openclaw/plugins/kefu-tools/` and are registered with OpenClaw. Four everyday read tools are available by default. The other capabilities require explicit opt-in.

| Tool | Purpose | Default / enable setting |
|---|---|---|
| `order_lookup` | Find an order by order ID or trusted customer identity | Enabled |
| `logistics_lookup` | Read shipment status and optional tracking events | Enabled |
| `refund_history_lookup` | Read refund history and risk information | Enabled |
| `refund_case_lookup` | Aggregate after-sales, order, logistics, identity, dialogue, and refund facts | Enabled |
| `aftersale_list_lookup` | Query after-sales cases in bulk | `CLAWBOT_ENABLE_ADMIN_QUERY_TOOLS=1` |
| `approve_refund_by_order_id` | Approve a small refund when backend checks allow it | `CLAWBOT_ENABLE_REFUND_WRITE_TOOL=1` |
| `blacklist_add` | Add an account to the blacklist | `CLAWBOT_ENABLE_BLACKLIST_WRITE_TOOLS=1` |
| `blacklist_remove` | Remove an account from the blacklist | `CLAWBOT_ENABLE_BLACKLIST_WRITE_TOOLS=1` |
| `batch_reclass` | Reclassify customer-service records in bulk | `CLAWBOT_ENABLE_OFFLINE_ADMIN_TOOLS=1` |
| `other_actionable_review` | Review unresolved actionable samples and produce statistics | `CLAWBOT_ENABLE_OFFLINE_ADMIN_TOOLS=1` |
| `image_inspect` | Compatibility path for damage checks, item comparison, and OCR | `CLAWBOT_ENABLE_IMAGE_INSPECT_TOOL=1`, plus vision configuration |
| `file_read` | Read text/JSON under approved roots | `CLAWBOT_ENABLE_FILE_READ_TOOL=1` + `CLAWBOT_FILE_READ_ROOTS` |
| `shell_exec` | Run approved operational commands | `CLAWBOT_ENABLE_SHELL_EXEC_TOOL=1` + `CLAWBOT_SHELL_ALLOWED_COMMANDS` |

Normal image messages are passed directly to OpenClaw's native vision input when possible. `image_inspect` is a compatibility path and remains unregistered by default.

The plugin discards model-supplied API addresses, keys, tokens, headers, and passwords. It also prevents the model from overriding the deployment-owned `CLAWBOT_TARGET_OUTER_ID`. Restart the OpenClaw gateway after changing tool-registration flags. See [`openclaw/plugins/kefu-tools/README.md`](openclaw/plugins/kefu-tools/README.md) for the exact boundary.

## Repository layout

```text
src/kefu_clawbot/                 Decision API, OpenClaw invocation, sessions, media, receipts, ops API
src/kefu_tool_backend/            Order/logistics/refund/identity/vision and administrative backends
src/kefu_identity_service/        Nickname-to-order identity service
openclaw/workspace-kefu-ops/      kefu_ops Agent rules, Skill, and setup script
openclaw/plugins/kefu-tools/      Thirteen OpenClaw tools and registration gates
workers/                          Browser, Appium, Android workers, and watchdog for six platforms
src/dxl_agent/                    Independent deterministic reliability and offline-Eval baseline
evals/, tests/                    Versioned Eval cases and unit/integration tests
config/, deploy/                  Policy, service, and deployment configuration
```

## Full installation and startup

### 1. Install the Python services

The host needs Python 3.11 or 3.12, `git`, `curl`, and `jq`. OpenClaw requires Node.js/npm. Browser workers also need Chrome/Chromium. Mobile workers require either Android SDK + ADB or Node.js + Appium, depending on the selected path. Docker is needed only for the containerized policy smoke path below.

```bash
git clone https://github.com/whichmen/dxl-commerce-agent.git
cd dxl-commerce-agent

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'

cp .env.example .env
```

Edit `.env` before starting:

- replace `CLAWBOT_API_KEY`, `CLAWBOT_TOOL_API_KEY`, and `IDENTITY_API_KEY` with different random values;
- keep `CLAWBOT_DECISION_MODE=agent`;
- configure an authorized ERP/order-system connection;
- enable identity mapping, vision, Enterprise WeChat, or high-risk tools only when needed;
- set each worker's `DECISION_KEY` to the Decision API's `CLAWBOT_API_KEY`.

### 2. Configure OpenClaw and an LLM

Install OpenClaw with its current recommended commands and finish onboarding first:

```bash
npm install -g openclaw@latest
openclaw onboard --install-daemon
bash scripts/check_requirements.sh core
```

Onboarding creates the OpenClaw configuration and connects a model/provider. It must finish before this repository's setup script is run. Model credentials are not hard-coded here.

Run `bash scripts/check_requirements.sh workers` before using browser workers, `bash scripts/check_requirements.sh mobile` for Android/Appium, or `bash scripts/check_requirements.sh all` to check every host dependency.

The `kefu-tools` plugin runs inside the OpenClaw Gateway process, so values in the repository root `.env` do not reach it automatically. Safely synchronize the 14 allowed `CLAWBOT_*` names needed by the plugin:

```bash
bash scripts/sync_openclaw_gateway_env.sh
```

By default, the script reads the root `.env` and writes the selected names to `~/.openclaw/.env`. It does not print values and preserves unrelated OpenClaw environment settings. See [`openclaw/gateway.env.example`](openclaw/gateway.env.example) for the complete list. The Gateway must receive `CLAWBOT_TOOL_API_KEY`, tool-registration flags, tool timeout, `CLAWBOT_TARGET_OUTER_ID`, and the file/command allowlists.

Then connect the included `kefu_ops` Agent, Skill, and tool plugin to OpenClaw:

```bash
bash openclaw/workspace-kefu-ops/scripts/setup_openclaw_kefu_ops.sh
openclaw gateway restart

openclaw skills info kefu-core
openclaw plugins info kefu-tools
openclaw agent --agent kefu_ops --session-id probe --message "reply only with ok" --local --json
```

The last command must return a model response, and its tool report should include `kefu-tools`. The setup script automatically loads the root `.env` and allows only `kefu-tools` by default. Set `CLAWBOT_ENABLE_OPENCLAW_BUILTINS=1` before setup only when the Agent also needs OpenClaw's general built-in tools. After changing any Gateway-facing value, run the synchronization script again and restart the gateway.

### 3. Start the services

The main startup script loads the repository root `.env`:

```bash
bash scripts/run_dev.sh
```

Check it from another terminal:

```bash
curl -sS http://127.0.0.1:18080/health
bash scripts/smoke_test.sh
```

`/health` should report `decision_mode=agent` and `openclaw_cli_available=true`. In Agent mode, `scripts/smoke_test.sh` goes through the configured model.

After the manual start and smoke check succeed, install the Decision API and active-path watchdog as user-level systemd units. The installer renders the current clone's absolute path and starts both units:

```bash
bash scripts/install_decision_user_services.sh
systemctl --user status kefu-clawbot.service
systemctl --user status openclaw-agent-bridge-kefu-ops-watchdog.timer
```

The watchdog reads authentication settings from the root `.env` and periodically checks the real Decision/Agent path. The OpenClaw Gateway remains managed by `openclaw onboard --install-daemon`.

Start the identity-mapping service separately when needed. The repository contains an invented CSV format sample. Copy it to a private path and provide at least `org_id`, `platform`, `platform_nickname`, and `km_name`; do not commit a real mapping file:

```bash
python scripts/import_identity_csv.py examples/identity_mappings.example.csv \
  --database data/identity.db

bash scripts/run_identity.sh
```

The example command imports invented values only, which is useful for checking the format. For real operation, replace the first argument with the private CSV and make `IDENTITY_DB_PATH` point to the same database file.

For an optional multi-application Enterprise WeChat administration channel, copy [`examples/wecom_hub.apps.example.json`](examples/wecom_hub.apps.example.json) to a private location, fill in your own application values, set `CLAWBOT_WECOM_HUB_CONFIG` in `.env`, and start it with:

```bash
bash scripts/run_wecom_hub.sh
```

This is an optional administration channel; the six commerce-platform customer-service workers do not depend on it.

### 4. Connect a platform worker

Douyin example:

```bash
cd workers
cp .env.example .env.local
chmod 600 .env.local
set -a
source .env.local
set +a
bash scripts/run_douyin_worker.sh
```

The script starts Chrome with a dedicated profile and CDP port. Sign in manually the first time; the private Chrome profile retains the session on that machine.

Multiple stores and workers can be declared in `workers/deploy/workers.watchdog.conf` and supervised by `workers/worker_watchdog.py` or systemd. See [`workers/README.md`](workers/README.md) for Taobao Appium, the Taobao/Xiaohongshu Android projects, and every other platform command.

## Docker Compose is the policy smoke path

```bash
docker compose up --build
curl -sS http://127.0.0.1:18080/health
```

The default Compose file explicitly sets `CLAWBOT_DECISION_MODE=policy`. The image does not bundle a personal OpenClaw model account or gateway, so this command checks the container, HTTP API, SQLite, configuration, and fixed-policy fallback only. **It does not call an LLM and is not the complete Agent path.**

Use the host setup above for the full model path, or build a deployment image containing your own OpenClaw gateway and model configuration.

## Data, memory, and RAG

- recent turns are read from SQLite and supplied as short-term context for the same OpenClaw session;
- orders, logistics, inventory, and refunds are changing facts and should come from authorized ERP/API tools, not model memory;
- a read-only RAG layer can be added for large product manuals, platform policies, and after-sales documentation, but it must not replace transaction tools or grant refund authority;
- RAG is not forced into the current transaction path, and multiple agents are not required for native tool calling.

## Default safety boundaries

- the Decision API, tool API, and identity service support separate keys;
- OpenClaw exposes only `kefu-tools` by default, without general file or command tools;
- nine high-risk or administrative tools are unregistered by default, and write tools have backend business checks;
- `file_read` and `shell_exec` still require an approved real-path root or command allowlist after their registration flags are enabled;
- platform cookies, browser profiles, ERP passwords, model keys, customer data, and machine-specific settings come from the deployment environment rather than hard-coded source;
- SQLite stores received messages, model replies, and delivery receipts. Set access controls, backups, retention, and deletion to match the data handled by a real deployment.

## Verification

```bash
python -m pytest
python -m dxl_agent.eval_runner --json
ruff check . --select E9,F63,F7,F82
python -m compileall -q src workers
python scripts/secret_scan.py
```

The current workspace validation reports **72 passing tests** and **50/50 passing deterministic offline Eval cases with 0 safety failures**. CI also covers Python 3.11/3.12, wheel packaging, high-confidence syntax checks, secret scanning, and container builds.

The automated suite covers the API, OpenClaw invocation boundary, tool authorization, identity service, and existing reliability components. CI has no personal model account, so it uses a controlled substitute at the OpenClaw boundary; run the steps above in your own OpenClaw environment to verify a live model response.

## My operating experience

I have built and operated private customer-service automation for **Taobao, Pinduoduo, Douyin Ecommerce, Kuaishou Ecommerce, Xiaohongshu, and WeChat Channels**, covering pre-sales, after-sales, browser-side, and mobile-side processing.

According to my operating records, routine pre-sales and after-sales work was handled automatically, with customer responses usually taking about 0.5–2.5 minutes. This is a summary of my actual operations, not a benchmark produced by this repository.

## Contact

For technical discussion or other questions, contact me on WeChat: `whichmen`.

## License

This project uses the [MIT License](LICENSE). Third-party services and commerce platforms still require their own account permissions, interfaces, and platform rules to be followed.
