# OpenClaw 客服 Agent 安装说明

`kefu_ops` 是客服主 Agent。大模型在这个 workspace 中读取规则和 Skill，通过 `kefu-tools` 查询订单、物流、退款和其他业务事实，然后生成最终回复。

## 组成

- `AGENTS.md`：只放必须遵守的硬约束；
- `skills/kefu-core/SKILL.md`：客服业务流程、工具选择和回复规则；
- `../plugins/kefu-tools/`：13 个模型可调用工具；
- `scripts/setup_openclaw_kefu_ops.sh`：把 workspace、Skill 和插件写入已有的 OpenClaw 配置。

插件只负责把模型工具调用转给 Decision API。订单/ERP、身份服务、视觉服务和写操作校验都在 Python 后端，所以完整运行必须同时启动仓库根目录的 Decision API。

## 1. 先安装和初始化 OpenClaw

```bash
npm install -g openclaw@latest
openclaw onboard --install-daemon
```

Onboarding 会创建 `~/.openclaw/openclaw.json` 并配置模型/provider。没有完成 onboarding 时，不要先跑下面的 setup 脚本。

## 2. 准备 Gateway 环境

先在仓库根目录从 `.env.example` 创建并填写私有 `.env`。因为插件运行在 OpenClaw Gateway 进程中，Gateway 必须单独拿到工具地址、工具密钥和注册开关：

```bash
cd /path/to/dxl-commerce-agent
bash scripts/sync_openclaw_gateway_env.sh
```

脚本默认把 14 个允许的 `CLAWBOT_*` 变量名从根 `.env` 同步到 `~/.openclaw/.env`，不打印值，也不删除其他 OpenClaw 环境配置。完整字段见 `openclaw/gateway.env.example`。

## 3. 注册 Agent、Skill 和插件

在仓库根目录运行：

```bash
bash openclaw/workspace-kefu-ops/scripts/setup_openclaw_kefu_ops.sh
openclaw gateway restart
```

setup 脚本会：

1. 自动读取仓库根目录 `.env`；
2. 创建或更新 `kefu_ops` Agent，并把 workspace 指向当前目录；
3. 加载同级 `openclaw/plugins/kefu-tools`；
4. 加载 `skills/kefu-core`；
5. 默认只允许 `kefu-tools`，不开放通用文件和命令工具。

确实需要 OpenClaw 通用内置工具时，应先在根 `.env` 设置 `CLAWBOT_ENABLE_OPENCLAW_BUILTINS=1`，重新同步 Gateway 环境，再重新运行 setup 和 gateway restart。

## 4. 验证模型和工具

```bash
openclaw skills info kefu-core
openclaw plugins info kefu-tools
openclaw agent --agent kefu_ops --session-id probe --message "只回复ok" --local --json
```

检查：

- Skill 能显示 `kefu-core`；
- Plugin 能显示 `kefu-tools`；
- Agent 命令能返回真实模型回复；
- JSON 里的工具报告能看到四个默认工具。

然后从仓库根目录启动 Decision API，并验证完整请求：

```bash
bash scripts/run_dev.sh
bash scripts/smoke_test.sh
```

## 5. 工具权限

默认注册四个日常只读工具：

- `order_lookup`
- `logistics_lookup`
- `refund_history_lookup`
- `refund_case_lookup`

另外九个工具需要独立开关。`file_read` 与 `shell_exec` 还需要后端路径/命令白名单。所有名称和注意事项见 [`../plugins/kefu-tools/README.md`](../plugins/kefu-tools/README.md)。

修改任一 Gateway 所需变量后都要重新执行：

```bash
bash scripts/sync_openclaw_gateway_env.sh
openclaw gateway restart
```

店铺名、账号、顾客映射、平台地址、ERP 密码、模型密钥和 Cookie 只应放在私有运行环境，不要写入 workspace 文件。
