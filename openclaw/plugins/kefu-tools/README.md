# kefu-tools

这个插件把客服业务工具注册给 OpenClaw Agent。插件只负责参数规范化和调用本地 Decision API；平台账号、顾客数据、业务系统地址及凭据由部署环境提供。

## 默认注册的工具

- `order_lookup`：按订单号或可信顾客身份查询订单摘要。
- `logistics_lookup`：查询物流状态和可选轨迹。
- `refund_history_lookup`：查询历史退款与风险信息。
- `refund_case_lookup`：聚合售后审核需要的订单、物流、退款和上下文事实。

## 默认关闭的高风险或管理工具

这些工具的代码仍保留，但只有对应环境变量为 `1`、`true`、`yes` 或 `on` 时才会注册：

| 工具 | 启用变量 |
|---|---|
| `aftersale_list_lookup` | `CLAWBOT_ENABLE_ADMIN_QUERY_TOOLS` |
| `approve_refund_by_order_id` | `CLAWBOT_ENABLE_REFUND_WRITE_TOOL` |
| `blacklist_add`, `blacklist_remove` | `CLAWBOT_ENABLE_BLACKLIST_WRITE_TOOLS` |
| `batch_reclass`, `other_actionable_review` | `CLAWBOT_ENABLE_OFFLINE_ADMIN_TOOLS` |
| `image_inspect` | `CLAWBOT_ENABLE_IMAGE_INSPECT_TOOL` |
| `file_read` | `CLAWBOT_ENABLE_FILE_READ_TOOL` |
| `shell_exec` | `CLAWBOT_ENABLE_SHELL_EXEC_TOOL` |

环境变量在插件注册时读取，修改后需要重启 OpenClaw gateway。生产环境还应在 Decision API 后端重复实施认证、授权、路径白名单、命令白名单和审计，不能只依赖插件是否注册。

### `file_read` 与 `shell_exec` 的后端边界

这两个工具采用“注册开关 + 后端白名单”的双重授权；只打开上表中的工具开关仍不能读取文件或执行命令：

- `CLAWBOT_FILE_READ_ROOTS`：允许读取的目录。Linux 下用 `:` 分隔多个目录，建议只配置绝对路径。后端会解析 `..` 和符号链接后的真实路径，目标必须仍位于允许目录内。
- `CLAWBOT_SHELL_ALLOWED_COMMANDS`：允许的命令或前缀，推荐使用带引号的 JSON 数组，例如 `'["git status","journalctl -u kefu-clawbot"]'`。规则允许完整精确匹配，也允许规则后接空白分隔的普通参数；前缀扩展不允许管道、重定向、命令替换或命令串联。确实需要复杂 shell 时，应把完整命令作为一条精确规则配置。不要把 `sh`、`bash`、`eval`、`python -c` 等解释器配置成宽泛前缀，否则效果接近任意命令执行。
- 兼容开关 `CLAWBOT_ADMIN_TOOLS_ENABLED=1` 可以启用两个后端实现，但不能绕过上述白名单。

将任一白名单配置为 `*` 会明确取消对应限制：文件读取可访问整个主机文件系统，shell 可执行任意命令；把文件根目录配置成 `/` 同样等于放开整个文件系统。这个模式只适合有额外进程/容器隔离、可随时销毁的环境，不应在生产主机使用。

## 特殊订单标记

`refund_case_lookup` 使用的特殊 `outer_id` 通过 `CLAWBOT_TARGET_OUTER_ID` 注入。模型不能自行传入或覆盖这个值；未配置时插件不会添加 `target_outer_id`。

## 网关配置

- `CLAWBOT_TOOL_BASE_URL`：Decision API 地址，默认 `http://127.0.0.1:18080`。
- `CLAWBOT_TOOL_API_KEY`：Decision API 鉴权密钥。
- `CLAWBOT_TOOL_TIMEOUT_SEC`：调用超时秒数，默认 8 秒。
