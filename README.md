# DXL Commerce Agent

这是我做的一套完整电商客服自动化系统。它不是靠规则 Planner 拼固定话术：默认主链路由 **OpenClaw + 大模型**理解顾客问题、结合上下文选择工具、查询真实业务数据，再生成可以直接发送的回复。

项目把平台消息接入、浏览器和手机操控、Agent、订单与物流工具、售后风控、会话记忆、发送回执、异常恢复、运行监控和 Eval 放在了一个仓库里。

[![CI](https://github.com/whichmen/dxl-commerce-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/whichmen/dxl-commerce-agent/actions/workflows/ci.yml)

[English](README_EN.md) · [架构](docs/architecture.md) · [客服链路](docs/customer-service-architecture.md) · [安全边界](docs/safety-model.md) · [实战经验](docs/production-lessons.md) · [安全报告](SECURITY.md)

## 能做什么

- 自动接收并回复售前、订单、物流、发货、质量、发错货、少件、退款和其他售后问题；
- 把六个平台的浏览器端、Appium 手机端和 Android 无障碍端消息统一成同一种事件；
- 让大模型根据 `kefu-core` 业务规则和最近对话自行判断是否查单、查物流、查退款记录或核验售后；
- 通过 OpenClaw 原生工具调用访问订单、物流、退款、身份映射和图像能力，不让模型凭记忆猜业务事实；
- 对每个会话串行处理，对消息做去重和结果缓存，发送完成后记录回执；
- 支持文字和图片消息、失败降级、Worker 的 `manual` 人工接管状态、健康检查和 watchdog 自动恢复；
- 用 SQLite 保存会话、决策、工具相关状态和发送结果，并提供运维查询接口；
- 用自动化测试、离线 Eval、密钥扫描和 CI 持续检查代码。

## 技术栈

下表列的是仓库中已经有实际代码和运行链路的技术，不是为了堆关键词列出的预计方案。需要额外服务或权限的能力已标为“可选”。

| 方向 | 实际使用的技术与工程能力 |
|---|---|
| 大模型与 Agent | OpenClaw、LLM、Agent Loop、原生 Function Calling / Tool Calling、Skill、Prompt 与业务规则 |
| Agent Memory | SQLite 最近对话、OpenClaw Session、按平台/店铺/用户维持会话上下文 |
| Agent 工具系统 | TypeScript OpenClaw 插件、JSON Schema、13 个业务工具、带鉴权的 HTTP Tool Gateway |
| 后端服务 | Python 3.11/3.12、FastAPI、Uvicorn、Pydantic v2、HTTP API |
| 并发与状态 | asyncio、按会话串行、消息去重、决策缓存、幂等键、发送回执 |
| 数据存储 | SQLite 客服对话、决策、身份映射、Worker 状态和发送结果 |
| 浏览器自动化 | Chrome CDP、Playwright Async API、独立浏览器 Profile、平台页面消息收发 |
| 手机自动化 | Appium、UiAutomator2、ADB、Android 端口反向代理 |
| Android 原生 | Kotlin、AccessibilityService、OkHttp、Kotlin Coroutines、本地配置界面 |
| 多模态 | OpenClaw 原生图片输入；可选 Qwen-VL、Hermes OCR、截图裁剪与 OCR |
| 电商业务系统 | ERP 接入、订单、物流、售后、退款、顾客身份映射 |
| 企业微信（可选） | 回调验签、AES 加解密、多应用 Hub、文字/图片消息推送 |
| 可靠性工程 | Worker watchdog、超时与失败恢复、健康检查、`manual` 人工接管协议、systemd |
| 测试与 Eval | pytest、pytest-asyncio、HTTPX/ASGI 集成测试、72 项自动化测试、50 条确定性离线 Eval |
| 工程化 | Ruff、Mypy、Secret Scan、Python wheel、Docker Compose、GitHub Actions |
| 安全控制 | 分离 API Key、工具权限开关、模型参数过滤、文件/命令白名单、退款后端校验 |

## 支持的平台

仓库里包含六个平台的接入代码，不只是接口定义。

| 平台 | 接入方式 | 主要代码 |
|---|---|---|
| 淘宝 | 千牛 App：Appium + UiAutomator2；另有 Android `AccessibilityService` | `workers/qianniu_mobile_worker.py`、`workers/android/qianniu-accessibility-worker/` |
| 拼多多 | 已登录 Chrome + CDP/Playwright，页面会话内增量收消息和发送 | `workers/pdd_auto_reply.py` |
| 抖音电商 | 飞鸽客服后台，Chrome CDP + Playwright | `workers/douyin_auto_reply.py` |
| 快手电商 | 快手小店客服后台，Chrome CDP + Playwright | `workers/kuaishou_auto_reply.py` |
| 小红书 | 千帆 App，Android `AccessibilityService` | `workers/android/qianfan-accessibility-worker/` |
| 微信视频号 | 视频号小店客服后台，已登录 Chrome + CDP/Playwright | `workers/weixin_auto_reply.py` |

平台登录 Cookie、设备授权和店铺配置只放在运行机器上。页面结构升级后，可能需要同步调整选择器或页面调用逻辑；每个平台的启动方法见 [`workers/README.md`](workers/README.md)。

## 模型到底负责什么

默认的 `CLAWBOT_DECISION_MODE=agent` 会走下面这条链路：

```mermaid
flowchart LR
    A[六个平台 Worker] --> B[统一 IncomingMessage]
    B --> C[Decision API /v1/decide]
    C --> D[会话锁、历史、媒体和渠道能力]
    D --> E[OpenClaw kefu_ops Agent]
    E --> F[大模型理解问题并规划]
    F --> G[原生调用 kefu-tools]
    G --> H[订单、物流、退款、身份和图像后端]
    H --> F
    F --> I[生成最终文字或图片回复]
    I --> J[Worker 发回平台]
    J --> K[/v1/worker/ack 记录回执]
```

这里的大模型不是装饰：

1. 它读当前消息、最近对话、平台和店铺上下文；
2. 它按 `openclaw/workspace-kefu-ops/skills/kefu-core/SKILL.md` 判断问题和处理顺序；
3. 需要业务事实时，它通过 OpenClaw 原生 Function Calling 调用工具；
4. 工具把订单、物流、退款或图像结果返回给模型；
5. 模型结合结果写出最终客服回复，Worker 再把回复发回平台。

`classifier.py` 只提供粗分类元数据，不负责写答案。`CLAWBOT_DECISION_MODE=policy` 是没有模型时的固定规则降级模式，也不是默认业务主链路。

当前是一套单 Agent 主链路。一次 OpenClaw Agent 调用内部可以连续经历多轮“模型判断 → 工具调用 → 工具结果 → 最终回复”，不需要为了使用模型和工具强行拆成多个 Agent。

## 13 个 Agent 工具

工具代码都在 `openclaw/plugins/kefu-tools/`，由 OpenClaw 注册给模型。默认只给 Agent 四个日常只读工具，其余能力必须按需要明确开启。

| 工具 | 用途 | 默认状态 / 开关 |
|---|---|---|
| `order_lookup` | 按订单号或可信顾客身份查询订单摘要 | 默认开启 |
| `logistics_lookup` | 查询物流状态和轨迹 | 默认开启 |
| `refund_history_lookup` | 查询历史退款和风险信息 | 默认开启 |
| `refund_case_lookup` | 聚合售后、订单、物流、身份、对话和退款事实 | 默认开启 |
| `aftersale_list_lookup` | 批量查询售后列表 | `CLAWBOT_ENABLE_ADMIN_QUERY_TOOLS=1` |
| `approve_refund_by_order_id` | 在后端规则允许时同意小额退款 | `CLAWBOT_ENABLE_REFUND_WRITE_TOOL=1` |
| `blacklist_add` | 加入黑名单 | `CLAWBOT_ENABLE_BLACKLIST_WRITE_TOOLS=1` |
| `blacklist_remove` | 移出黑名单 | `CLAWBOT_ENABLE_BLACKLIST_WRITE_TOOLS=1` |
| `batch_reclass` | 批量重分类客服记录 | `CLAWBOT_ENABLE_OFFLINE_ADMIN_TOOLS=1` |
| `other_actionable_review` | 复审待处理样本并输出统计 | `CLAWBOT_ENABLE_OFFLINE_ADMIN_TOOLS=1` |
| `image_inspect` | 兼容图像核验、同款对比和 OCR | `CLAWBOT_ENABLE_IMAGE_INSPECT_TOOL=1`，同时配置视觉服务 |
| `file_read` | 读取白名单目录内的文本/JSON | `CLAWBOT_ENABLE_FILE_READ_TOOL=1` + `CLAWBOT_FILE_READ_ROOTS` |
| `shell_exec` | 执行白名单里的运维命令 | `CLAWBOT_ENABLE_SHELL_EXEC_TOOL=1` + `CLAWBOT_SHELL_ALLOWED_COMMANDS` |

普通图片消息优先直接作为附件交给 OpenClaw 的原生看图能力；`image_inspect` 是兼容能力，默认不注册。

插件不会接受模型传入的 API 地址、密钥、Token、Header 或密码，也不会让模型覆盖部署方配置的 `CLAWBOT_TARGET_OUTER_ID`。修改工具开关后需要重启 OpenClaw gateway。完整边界见 [`openclaw/plugins/kefu-tools/README.md`](openclaw/plugins/kefu-tools/README.md)。

## 代码结构

```text
src/kefu_clawbot/                 Decision API、OpenClaw 调用、会话、媒体、回执和运维接口
src/kefu_tool_backend/            订单/物流/退款/身份/图像及管理工具后端
src/kefu_identity_service/        昵称与订单身份映射服务
openclaw/workspace-kefu-ops/      kefu_ops Agent 的规则、Skill 和安装脚本
openclaw/plugins/kefu-tools/      13 个 OpenClaw 工具及权限开关
workers/                          六个平台的浏览器、Appium、Android Worker 和 watchdog
src/dxl_agent/                    独立的确定性可靠性与离线评估基线
evals/、tests/                    固定场景 Eval 和单元/集成测试
config/、deploy/                  策略、服务和部署配置
```

## 完整安装和启动

### 1. 安装 Python 部分

需要 Python 3.11 或 3.12、`git`、`curl` 和 `jq`。OpenClaw 需要 Node.js/npm；浏览器 Worker 还需要 Chrome/Chromium；手机端需要按所选方案准备 Android SDK、ADB，或 Node.js + Appium。Docker 只在运行后面的容器策略冒烟链路时需要。

```bash
git clone https://github.com/whichmen/dxl-commerce-agent.git
cd dxl-commerce-agent

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'

cp .env.example .env
```

编辑 `.env`，至少处理这些内容：

- 把 `CLAWBOT_API_KEY`、`CLAWBOT_TOOL_API_KEY` 和 `IDENTITY_API_KEY` 换成不同的随机值；
- 保持 `CLAWBOT_DECISION_MODE=agent`；
- 填写经过授权的 ERP/订单系统连接信息；
- 如果需要身份映射、视觉、企业微信或高风险工具，再打开对应配置；
- Worker 的 `DECISION_KEY` 必须和 Decision API 的 `CLAWBOT_API_KEY` 一致。

### 2. 配置 OpenClaw 和模型

先按 OpenClaw 当前推荐方式安装并完成 onboarding：

```bash
npm install -g openclaw@latest
openclaw onboard --install-daemon
bash scripts/check_requirements.sh core
```

Onboarding 会创建 OpenClaw 配置并接入模型/provider；必须先完成这一步，再运行本仓库的 setup 脚本。模型账号和密钥不写进这个仓库。

需要浏览器 Worker 时再运行 `bash scripts/check_requirements.sh workers`；需要 Android/Appium 时运行 `bash scripts/check_requirements.sh mobile`；一次检查全部宿主依赖可以用 `bash scripts/check_requirements.sh all`。

`kefu-tools` 插件运行在 OpenClaw Gateway 进程里，所以只把变量写在仓库根目录 `.env` 还不够。把插件需要的 14 个 `CLAWBOT_*` 变量安全同步到 Gateway 环境：

```bash
bash scripts/sync_openclaw_gateway_env.sh
```

脚本默认从根目录 `.env` 读取允许的变量名，写到 `~/.openclaw/.env`，不会打印变量值，也会保留 OpenClaw 原有的其他环境变量。字段清单见 [`openclaw/gateway.env.example`](openclaw/gateway.env.example)。`CLAWBOT_TOOL_API_KEY`、工具注册开关、工具超时、`CLAWBOT_TARGET_OUTER_ID` 以及文件/命令白名单都必须让 Gateway 进程看见。

然后把仓库里的 `kefu_ops` Agent、Skill 和工具插件接入当前 OpenClaw：

```bash
bash openclaw/workspace-kefu-ops/scripts/setup_openclaw_kefu_ops.sh
openclaw gateway restart

openclaw skills info kefu-core
openclaw plugins info kefu-tools
openclaw agent --agent kefu_ops --session-id probe --message "只回复ok" --local --json
```

最后一条能返回模型回复，并且结果里的工具列表能看到 `kefu-tools`，才说明模型主链路已经接通。setup 脚本会自动读取根 `.env`；它默认只把 `kefu-tools` 给这个 Agent，只有 `CLAWBOT_ENABLE_OPENCLAW_BUILTINS=1` 才会额外放开 OpenClaw 通用内置工具。以后修改 Gateway 所需变量时，重新运行同步脚本并重启 gateway。

### 3. 启动服务

主服务会自动读取仓库根目录的 `.env`：

```bash
bash scripts/run_dev.sh
```

另开终端检查：

```bash
curl -sS http://127.0.0.1:18080/health
bash scripts/smoke_test.sh
```

`/health` 中应看到 `decision_mode` 为 `agent`，并确认 `openclaw_cli_available` 为 `true`。`scripts/smoke_test.sh` 在 Agent 模式下会真实经过已经配置的模型。

确认手工启动和 smoke 都正常后，可以把 Decision API 和主动探活 watchdog 安装成当前用户的 systemd 服务。脚本会按当前 clone 的绝对路径渲染 unit，并立即启动：

```bash
bash scripts/install_decision_user_services.sh
systemctl --user status kefu-clawbot.service
systemctl --user status openclaw-agent-bridge-kefu-ops-watchdog.timer
```

这个 watchdog 会从根 `.env` 读取鉴权配置，定时检查真正的 Decision/Agent 路径；OpenClaw Gateway 仍由前面的 `openclaw onboard --install-daemon` 管理。

身份映射服务按需单独启动。仓库提供了虚构数据格式样例；先复制成自己的私有 CSV，至少填写 `org_id`、`platform`、`platform_nickname` 和 `km_name`，不要把真实映射文件提交到 Git：

```bash
python scripts/import_identity_csv.py examples/identity_mappings.example.csv \
  --database data/identity.db

bash scripts/run_identity.sh
```

上面的样例命令只会导入虚构记录，用来检查格式。正式运行时把第一个参数换成自己的私有 CSV，并让 `IDENTITY_DB_PATH` 指向同一个数据库文件。

如果要用企业微信做多应用的管理入口，先把 [`examples/wecom_hub.apps.example.json`](examples/wecom_hub.apps.example.json) 复制到私有路径，填入自己的企业微信应用参数，再在 `.env` 中设置 `CLAWBOT_WECOM_HUB_CONFIG` 并启动：

```bash
bash scripts/run_wecom_hub.sh
```

这是可选管理通道；六个电商平台的客服收发不依赖它。

### 4. 接入平台 Worker

以抖音为例：

```bash
cd workers
cp .env.example .env.local
chmod 600 .env.local
set -a
source .env.local
set +a
bash scripts/run_douyin_worker.sh
```

脚本会启动带独立用户目录和 CDP 端口的 Chrome。第一次需要手工登录平台，之后登录状态保存在本机的私有 Chrome profile 中。

多个店铺或多个 Worker 可以统一写入 `workers/deploy/workers.watchdog.conf`，再用 `workers/worker_watchdog.py` 或 systemd 管理。淘宝 Appium、淘宝/小红书 Android 工程以及其他平台的完整命令见 [`workers/README.md`](workers/README.md)。

## Docker Compose 是策略冒烟链路

```bash
docker compose up --build
curl -sS http://127.0.0.1:18080/health
```

默认 Compose 明确设置 `CLAWBOT_DECISION_MODE=policy`。镜像没有打包个人的 OpenClaw 模型账号和 gateway，所以这条命令只检查容器、HTTP、SQLite、配置和固定策略降级链路，**不会经过大模型，也不能代表完整 Agent 链路**。

要跑完整模型链路，请按上面的本机方式配置 OpenClaw，或者制作包含自己 OpenClaw gateway 和模型配置的部署镜像。

## 数据、记忆和 RAG

- 最近会话会从 SQLite 读出，作为短期上下文交给同一个 OpenClaw 会话；
- 订单、物流、库存和退款是不断变化的事实，应该由有权限的工具查询 ERP/API，不能让模型靠记忆回答；
- 商品资料、平台政策和售后说明如果很多，可以再接只读 RAG，但 RAG 不应该替代订单工具，也不能授予退款权限；
- 当前主交易链路没有强行加入 RAG，多 Agent 也不是完成工具调用的前提。

## 默认安全边界

- Decision API、工具 API 和身份服务分别支持独立密钥；
- OpenClaw 默认只允许 `kefu-tools`，通用文件和命令工具不开放；
- 九个高风险或管理工具默认不注册，写操作在后端还有业务校验；
- `file_read` 和 `shell_exec` 即使打开注册开关，也必须再配置真实路径根目录或命令白名单；
- 平台 Cookie、浏览器 profile、ERP 密码、模型密钥、顾客数据和本机配置由部署环境注入，不写死在源码；
- SQLite 会保存收到的消息、模型回复和发送回执。接入真实顾客前，应根据自己的数据要求设置访问权限、备份、保留期限和清理流程。

## 验证

```bash
python -m pytest
python -m dxl_agent.eval_runner --json
ruff check . --select E9,F63,F7,F82
python -m compileall -q src workers
python scripts/secret_scan.py
```

当前工作区复测结果是 **72 passed**，确定性离线 Eval 是 **50/50 passed，0 safety failures**。CI 同时覆盖 Python 3.11/3.12、wheel 构建、关键语法检查、密钥扫描和容器构建。

这组自动化检查覆盖 API、OpenClaw 调用边界、工具权限、身份服务和原有可靠性逻辑。CI 没有个人模型账号，所以 OpenClaw 返回在测试中使用受控替身；真正的模型回复还需要按上面的步骤在自己的 OpenClaw 环境里验证。

## 我之前做过的实际业务

我实际做过覆盖 **淘宝、拼多多、抖音电商、快手电商、小红书和微信视频号** 的私有电商客服自动化系统，包括售前、售后、浏览器端和手机端处理。

按我的实际运行记录，基础售前、售后客服工作实现了自动处理，客服响应时间通常在 0.5～2.5 分钟。这里列的是我对实际业务的汇总，不是公开仓库的压测结果。

## 联系我

技术交流或其他问题联系我，微信：`whichmen`。

## 许可证

项目使用 [MIT License](LICENSE)。使用第三方服务和电商平台时，还需要遵守各自的账号权限、接口和平台规则。
