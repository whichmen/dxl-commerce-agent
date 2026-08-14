type JsonObject = Record<string, unknown>;
type ToolDefinition = {
  name: string;
  description: string;
  properties?: Record<string, unknown>;
};

const HIGH_RISK_TOOL_FLAGS: Record<string, string> = {
  aftersale_list_lookup: "CLAWBOT_ENABLE_ADMIN_QUERY_TOOLS",
  approve_refund_by_order_id: "CLAWBOT_ENABLE_REFUND_WRITE_TOOL",
  blacklist_add: "CLAWBOT_ENABLE_BLACKLIST_WRITE_TOOLS",
  blacklist_remove: "CLAWBOT_ENABLE_BLACKLIST_WRITE_TOOLS",
  batch_reclass: "CLAWBOT_ENABLE_OFFLINE_ADMIN_TOOLS",
  other_actionable_review: "CLAWBOT_ENABLE_OFFLINE_ADMIN_TOOLS",
  image_inspect: "CLAWBOT_ENABLE_IMAGE_INSPECT_TOOL",
  file_read: "CLAWBOT_ENABLE_FILE_READ_TOOL",
  shell_exec: "CLAWBOT_ENABLE_SHELL_EXEC_TOOL",
};

function envFlag(name: string): boolean {
  const value = String(process.env[name] || "").trim().toLowerCase();
  return ["1", "true", "yes", "on"].includes(value);
}

function isToolEnabled(toolName: string): boolean {
  const flagName = HIGH_RISK_TOOL_FLAGS[toolName];
  return !flagName || envFlag(flagName);
}

function toTextResult(payload: unknown) {
  return {
    content: [{ type: "text", text: JSON.stringify(payload, null, 2) }],
    details: payload as JsonObject,
  };
}

function normalizeBaseUrl(raw: unknown): string {
  const fromCfg = typeof raw === "string" ? raw.trim() : "";
  const fromEnv = String(process.env.CLAWBOT_TOOL_BASE_URL || "").trim();
  const value = fromCfg || fromEnv || "http://127.0.0.1:18080";
  return value.replace(/\/+$/, "");
}

function normalizeApiKey(raw: unknown): string {
  const fromCfg = typeof raw === "string" ? raw.trim() : "";
  const fromEnv = String(process.env.CLAWBOT_TOOL_API_KEY || "").trim();
  return fromCfg || fromEnv;
}

function normalizeTimeoutMs(raw: unknown): number {
  const fromCfg = Number(raw);
  if (Number.isFinite(fromCfg) && fromCfg >= 500) {
    return Math.min(120000, fromCfg);
  }
  const fromEnv = Number(process.env.CLAWBOT_TOOL_TIMEOUT_SEC || "8");
  if (Number.isFinite(fromEnv) && fromEnv > 0) {
    return Math.min(120000, Math.max(500, fromEnv * 1000));
  }
  return 8000;
}

function buildEvent(params: JsonObject): JsonObject {
  const raw = params.event;
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    return raw as JsonObject;
  }
  const event: JsonObject = {};
  const keys = [
    "platform",
    "store_id",
    "store_name",
    "platform_nickname",
    "text",
    "media_id",
    "media_url",
    "media_path",
    "message_type",
  ];
  for (const key of keys) {
    const val = params[key];
    if (val === undefined || val === null) continue;
    event[key] = val;
  }
  return event;
}

function buildArgs(params: JsonObject): JsonObject {
  const args = { ...params };
  delete args.event;
  // Connection and credential fields belong to trusted plugin configuration.
  // Never forward model-supplied overrides to a backend tool.
  for (const key of ["base_url", "api_key", "token", "secret", "password", "headers"]) {
    delete args[key];
  }
  if (typeof args.order_id !== "string" || !args.order_id.trim()) {
    const alias = firstNonEmptyString(args.tid, args.platform_order_id);
    if (alias) {
      args.order_id = alias;
    }
  }
  return args;
}

function normalizeRefundCasePayload(args: JsonObject): JsonObject {
  const normalized = { ...args };
  // The deployment-specific outer ID is trusted configuration, not model input.
  delete normalized.target_outer_id;
  const configured = String(process.env.CLAWBOT_TARGET_OUTER_ID || "").trim();
  if (configured) {
    normalized.target_outer_id = configured;
  }
  return normalized;
}

function firstNonEmptyString(...vals: unknown[]): string {
  for (const v of vals) {
    if (typeof v !== "string") continue;
    const s = v.trim();
    if (s) return s;
  }
  return "";
}

function extractMediaIdFallback(...vals: unknown[]): string {
  for (const v of vals) {
    if (typeof v !== "string") continue;
    const s = v.trim();
    if (!s) continue;
    if (/^media_[a-zA-Z0-9]+$/.test(s)) return s;
    const m = s.match(/\bmedia_[a-zA-Z0-9]+\b/);
    if (m) return m[0];
  }
  return "";
}

function normalizeImageInspectPayload(args: JsonObject, event: JsonObject): JsonObject {
  const normalized = { ...args };
  const mediaId = firstNonEmptyString(
    normalized.media_id,
    event.media_id,
    extractMediaIdFallback(normalized.image_url),
    extractMediaIdFallback(normalized.customer_text),
    extractMediaIdFallback(event.text),
  );
  if (mediaId) {
    normalized.media_id = mediaId;
  }
  const mediaPath = firstNonEmptyString(
    normalized.media_path,
    event.media_path,
    normalized.image_url,
    normalized.media_url,
    event.media_url,
    (!mediaId ? normalized.reference_url : ""),
    (!mediaId ? normalized.reference_image_url : ""),
  );
  if (mediaPath) {
    normalized.media_path = mediaPath;
  }
  return normalized;
}

async function callGatewayTool(opts: {
  baseUrl: string;
  apiKey: string;
  timeoutMs: number;
  toolName: string;
  payload: JsonObject;
}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), opts.timeoutMs);
  try {
    const headers: Record<string, string> = {
      "Content-Type": "application/json; charset=utf-8",
      Accept: "application/json",
    };
    if (opts.apiKey) {
      headers["X-Api-Key"] = opts.apiKey;
    }

    const resp = await fetch(`${opts.baseUrl}/v1/tools/${opts.toolName}`, {
      method: "POST",
      headers,
      body: JSON.stringify(opts.payload),
      signal: controller.signal,
    });
    const text = await resp.text();
    const parsed = text ? JSON.parse(text) : {};
    if (!resp.ok) {
      throw new Error(`gateway_http_${resp.status}`);
    }
    if (!parsed || typeof parsed !== "object") {
      throw new Error("gateway_invalid_json");
    }
    return parsed as JsonObject;
  } finally {
    clearTimeout(timer);
  }
}

export default function register(api: any) {
  const toolDefs: ToolDefinition[] = [
    {
      name: "order_lookup",
      description: "按订单号或顾客身份查订单摘要（近90天）。",
      properties: {
        order_id: { type: "string" },
        tid: { type: "string" },
        platform_order_id: { type: "string" },
        platform: { type: "string" },
        store_name: { type: "string" },
        platform_nickname: { type: "string" },
        km_name: { type: "string" },
        lookback_days: { type: "number" },
        limit: { type: "number" },
        room_name: { type: "string" },
      },
    },
    {
      name: "logistics_lookup",
      description: "按订单号查询物流状态，可选返回物流轨迹。",
      properties: {
        order_id: { type: "string" },
        tid: { type: "string" },
        platform_order_id: { type: "string" },
        detail_level: { type: "string", enum: ["summary", "trace"] },
        lookback_days: { type: "number" },
        shop_name: { type: "string" },
      },
    },
    {
      name: "aftersale_list_lookup",
      description: "批量查询售后列表，可筛选未解决、已发货仅退款等条件。",
      properties: {
        shop_name: { type: "string" },
        store_name: { type: "string" },
        lookback_days: { type: "number" },
        limit: { type: "number" },
        page_size: { type: "number" },
        only_open: { type: "boolean" },
        required_aftersale_type: { type: "string" },
        required_status_text: { type: "string" },
      },
    },
    {
      name: "refund_history_lookup",
      description: "查询顾客历史退款记录和退款风险。",
      properties: {
        platform: { type: "string" },
        platform_nickname: { type: "string" },
        km_name: { type: "string" },
        lookback_days: { type: "number" },
        limit: { type: "number" },
      },
    },
    {
      name: "refund_case_lookup",
      description: "聚合售后审核事实包：售后、礼物单、订单、物流、名字映射、聊天上下文、退款历史。",
      properties: {
        as_id: { type: "string" },
        aftersale_id: { type: "string" },
        sid: { type: "string" },
        order_id: { type: "string" },
        tid: { type: "string" },
        platform_order_id: { type: "string" },
        platform: { type: "string" },
        shop_name: { type: "string" },
        store_name: { type: "string" },
        refundMoney: { type: "number" },
        status_text: { type: "string" },
        statusText: { type: "string" },
        platform_status_text: { type: "string" },
        platformStatusText: { type: "string" },
        aftersale_type: { type: "string" },
        afterSaleTypeText: { type: "string" },
        aftersale_reason: { type: "string" },
        reason: { type: "string" },
        reasonText: { type: "string" },
        platform_nickname: { type: "string" },
        km_name: { type: "string" },
        dxl_nickname: { type: "string" },
        room_name: { type: "string" },
        user_id: { type: "string" },
        lookback_days: { type: "number" },
        dialogue_limit: { type: "number" },
        refund_history_limit: { type: "number" },
      },
    },
    {
      name: "approve_refund_by_order_id",
      description: "按平台订单号执行同意退款。仅允许已发货仅退款，且平台实退金额不得超过5元。",
      properties: {
        order_id: { type: "string" },
        tid: { type: "string" },
        platform_order_id: { type: "string" },
        shop_name: { type: "string" },
        lookback_days: { type: "number" },
        max_refund_amount_yuan: { type: "number" },
        required_aftersale_type: { type: "string" },
      },
    },
    {
      name: "blacklist_add",
      description: "将指定旺旺ID加入黑名单。",
      properties: {
        org_id: { type: "string" },
        wangwang_id: { type: "string" },
        days: { type: "number" },
      },
    },
    {
      name: "blacklist_remove",
      description: "将指定旺旺ID移出黑名单。",
      properties: {
        org_id: { type: "string" },
        wangwang_id: { type: "string" },
      },
    },
    {
      name: "image_inspect",
      description: "图像核验：破损识别、同款对比、编码/文字识别。",
      properties: {
        task: {
          type: "string",
          enum: ["damage_check", "same_item_compare", "ocr_code", "read_code", "ocr_text"],
        },
        media_id: { type: "string" },
        media_path: { type: "string" },
        media_url: { type: "string" },
        image_url: { type: "string" },
        reference_image_url: { type: "string" },
        reference_url: { type: "string" },
        customer_text: { type: "string" },
      },
    },
    {
      name: "batch_reclass",
      description: "批量重分类客服聊天数据（管理用途）。",
      properties: {
        input_path: { type: "string" },
        output_json_path: { type: "string" },
        output_md_path: { type: "string" },
        max_items: { type: "number" },
      },
    },
    {
      name: "other_actionable_review",
      description: "复审 other_actionable 样本并输出统计（管理用途）。",
      properties: {
        input_path: { type: "string" },
        output_json_path: { type: "string" },
        output_md_path: { type: "string" },
        max_items: { type: "number" },
        sample_count: { type: "number" },
        focus_detail_category: { type: "string" },
        include_full_record: { type: "boolean" },
      },
    },
    {
      name: "file_read",
      description: "读取本地文本/JSON文件（管理用途）。",
      properties: {
        path: { type: "string" },
        max_chars: { type: "number" },
      },
    },
    {
      name: "shell_exec",
      description: "执行受限 shell 命令（管理用途）。",
      properties: {
        command: { type: "string" },
        timeout_sec: { type: "number" },
      },
    },
  ];

  for (const def of toolDefs.filter((item) => isToolEnabled(item.name))) {
    api.registerTool(
      {
        name: def.name,
        description: def.description,
        parameters: {
          type: "object",
          additionalProperties: false,
          properties: {
            ...(def.properties || {}),
            event: { type: "object", additionalProperties: true },
          },
        },
        async execute(_id: string, params: JsonObject) {
          const pluginCfg = (api?.pluginConfig || {}) as JsonObject;
          const apiKey = normalizeApiKey(pluginCfg.apiKey);
          const timeoutMs = normalizeTimeoutMs(pluginCfg.timeoutMs);

          const event = buildEvent(params);
          const rawArgs = buildArgs(params);
          let baseUrl = normalizeBaseUrl(pluginCfg.baseUrl);
          let args = rawArgs;
          if (def.name === "image_inspect") {
            args = normalizeImageInspectPayload(rawArgs, event);
          } else if (def.name === "refund_case_lookup") {
            args = normalizeRefundCasePayload(rawArgs);
          }
          const payload = { args, event };
          const result = await callGatewayTool({
            baseUrl,
            apiKey,
            timeoutMs,
            toolName: def.name,
            payload,
          });
          return toTextResult(result);
        },
      },
    );
  }
}
