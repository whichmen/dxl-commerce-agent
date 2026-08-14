# kefu-workers (Ubuntu)

Ubuntu 本地 worker（抖音/拼多多/视频号/快手/千牛手机客户端），以及千牛、小红书千帆的 Android AccessibilityService worker。

这些 Worker 负责收消息、调用 Decision API、把结果发回平台和记录发送回执；它们自己不靠关键词生成客服答案。正常运行时，根目录 Decision API 使用 `CLAWBOT_DECISION_MODE=agent`，由 OpenClaw + 大模型理解问题、调用业务工具并写最终回复。先按根目录 [`README.md`](../README.md) 配好模型和服务，再启动这里的平台 Worker。

## 目录

- `douyin_auto_reply.py`：抖音客服 worker
- `pdd_auto_reply.py`：拼多多客服 worker
- `weixin_auto_reply.py`：视频号客服 worker
- `kuaishou_auto_reply.py`：快手客服 worker
- `qianniu_mobile_worker.py`：淘宝千牛手机客户端 worker（Appium + UiAutomator2）
- `core/`：worker 公共模块
- `scripts/start_chrome_9222.sh`：启动/探活本机 Chrome CDP
- `scripts/run_douyin_worker.sh`：启动 worker（调用上面脚本）
- `scripts/run_pdd_worker.sh`：启动拼多多 worker（调用上面脚本）
- `scripts/run_weixin_worker.sh`：启动视频号 worker（调用上面脚本）
- `scripts/run_kuaishou_worker.sh`：启动快手 worker（调用上面脚本）
- `scripts/run_qianniu_mobile_worker.sh`：启动千牛手机客户端 worker（Appium）
- `scripts/install_appium_runtime.sh`：安装 Appium 本地运行时
- `android/qianniu-accessibility-worker/`：千牛 Android 无障碍 worker
- `android/qianfan-accessibility-worker/`：小红书千帆 Android 无障碍 worker
- `deploy/workers.watchdog.conf`：唯一 worker 配置文件（增减 worker 只改这里）
- `deploy/kefu-workers-manager.service`：systemd 管理 watchdog
- `deploy/kefu-workers-manager.path`：监听配置变更
- `deploy/kefu-workers-manager-reload.service`：配置变化时重启 manager

## 首次准备

1. 从示例创建本地环境配置（不要提交 `.env.local`）：

```bash
cd workers
cp .env.example .env.local
chmod 600 .env.local
# 填入本机的 STORE_ID / STORE_NAME / DECISION_KEY 等配置
set -a
source .env.local
set +a
```

2. 首次登录（仅一次）：

- 先手动运行 `scripts/start_chrome_9222.sh`
- 打开 `TARGET_URL`，扫码登录抖音客服后台
- 关闭后会话保存在 `CHROME_USER_DATA_DIR`
- `CHROME_USER_DATA_DIR` 含登录 Cookie/会话，只应保存在运行机，不要复制到导出包或提交到 Git。
- 如使用淘宝自动登录，先复制 `taobao_accounts.example` 到私有路径，设置 `chmod 600`，再用 `TAOBAO_ACCOUNTS_CONFIG` 指向它。

## 手工运行（调试）

```bash
cd workers
bash scripts/run_douyin_worker.sh
```

## Watchdog（多 worker，一处改配置）

```bash
cd workers
python3 worker_watchdog.py
```

- 只改一个文件：`deploy/workers.watchdog.conf`
- 一行一个 worker，注释一行即停用该 worker
- 浏览器 worker 可用 3 列简写：`kind|operator|store_name`
- 移动端 guard 可用 4 列简写：`kind|operator|store_name|mobile_udid`
- 平台名统一使用：`douyin / pdd / weixin / kuaishou / taobao / xiaohongshu`
- 简写会自动生成：
  - `key = {kind}_{operator}`
  - `store_id = {kind}_{store_name}`
  - 浏览器 worker：`REMOTE_PORT`、`CHROME_USER_DATA_DIR`
  - `STATE_DB = state/{kind}_worker_{operator}.db`

## 手工运行拼多多（调试）

```bash
cd workers
bash scripts/run_pdd_worker.sh
```

## 手工运行千牛手机客户端（调试）

```bash
cd workers
MOBILE_UDID=你的设备序列号 bash scripts/run_qianniu_mobile_worker.sh
```

说明：
- 千牛 mobile worker 走 Appium + UiAutomator2，不依赖 OCR。
- 默认连接 `http://127.0.0.1:4723`，若服务未启动会自动拉起本地 appium server。

首次安装（仅一次）：

```bash
cd workers
bash scripts/install_appium_runtime.sh
```

## systemd（推荐）

```bash
cd workers
bash scripts/install_workers_manager_service.sh
```

生效后：
- `kefu-workers-manager.service` 负责守护 watchdog
- watchdog 按 `deploy/workers.watchdog.conf` 拉起对应 worker
- 修改配置文件后，`kefu-workers-manager.path` 会自动触发重载

## 模拟 worker（不连平台页面）

用于压测/回归 `worker -> /v1/decide -> 回复` 链路，支持切换多个虚构测试顾客：

```bash
cd workers
python3 scripts/simulate_worker_douyin.py
```

常用命令：

- `/list`：查看内置测试顾客
- `/use <alias|index>`：切换顾客
- 直接输入文本：发送文本消息
- `/image <url> [text]`：发送图片消息
- `/newsession`：当前顾客切新 session

## 常用检查

```bash
curl -sS http://127.0.0.1:18080/health
curl -sS http://127.0.0.1:9222/json/version
sudo systemctl status --no-pager kefu-workers-manager.service
sudo systemctl status --no-pager kefu-workers-manager.path
tail -n 100 state/logs/worker_watchdog.log
```

## Android 工程配置

两套 Android 工程都不包含本机 `local.properties`。构建前在相应工程目录执行：

```bash
cp local.properties.example local.properties
# 然后把 sdk.dir 改为本机 Android SDK 路径
```

店铺、Decision URL 和可选 API Key 在 App 的配置页或 guard 同步配置中提供；源码内只保留空密钥和虚构店铺示例。

## 回滚（切回 Windows worker）

```bash
sudo systemctl stop kefu-workers-manager.path
sudo systemctl stop kefu-workers-manager.service
```
