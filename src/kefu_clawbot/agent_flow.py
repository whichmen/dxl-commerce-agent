from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from .models import CategoryResult, DecisionInternal, IncomingEvent
from .openclaw_agent import OpenClawAgentInvoker
from .tool_router import SUPPORTED_TOOLS, ToolResult, ToolRouter

FORBIDDEN_CONTACT_KEYWORDS = (
    "手机号",
    "手机号码",
    "电话",
    "联系方式",
    "微信",
    "vx",
    "v信",
    "加我",
    "私聊",
)

UNVERIFIED_CLAIM_KEYWORDS = (
    "已查询",
    "已经查询",
    "已核实",
    "已经核实",
    "已帮您查",
    "帮您查到",
    "已催件",
)

SAFE_CUSTOMER_MISSING_INFO_REPLY = (
    "为尽快核实处理，请提供订单号和下单时间（无需手机号），"
    "我这边马上为您跟进。"
)
SAFE_ADMIN_MISSING_INFO_REPLY = "我这边继续执行，请稍等；如需重跑请直接重发完整指令。"
SAFE_GENERIC_MISSING_INFO_REPLY = "请补充必要信息，我马上继续处理。"
IMAGE_URL_MARKER_RE = re.compile(r"\[\[\s*IMAGE_URL\s*:\s*(https?://[^\]\s]+)\s*\]\]", re.IGNORECASE)
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*]\((https?://[^)\s]+)\)")


@dataclass
class PlanDecision:
    intent: str
    confidence: float
    need_tool: bool
    tool_name: str
    tool_args: dict[str, Any] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    ask_user: str = ""
    direct_reply: str = ""
    notes: str = ""


class AgentDecisionEngine:
    def __init__(
        self,
        *,
        invoker: OpenClawAgentInvoker,
        tool_router: ToolRouter,
        fallback_text: str,
    ) -> None:
        self.invoker = invoker
        self.tool_router = tool_router
        self.fallback_text = fallback_text
        self._locks_guard = Lock()
        self._session_locks: dict[str, Lock] = {}

    def decide(
        self,
        *,
        event: IncomingEvent,
        category_result: CategoryResult,
        trace_id: str,
        history: list[dict[str, Any]],
    ) -> DecisionInternal:
        session_lock = self._acquire_session_lock(event.session_key())
        with session_lock:
            return self._decide_locked(
                event=event,
                category_result=category_result,
                trace_id=trace_id,
                history=history,
            )

    def _decide_locked(
        self,
        *,
        event: IncomingEvent,
        category_result: CategoryResult,
        trace_id: str,
        history: list[dict[str, Any]],
    ) -> DecisionInternal:
        # Use per-trace session keys to avoid long-lived OpenClaw session drift.
        session_base = self.invoker.build_session_key(event)
        trace_suffix = (trace_id or "na")[:10]
        plan_session = f"{session_base}_{trace_suffix}_plan"
        reply_session = f"{session_base}_{trace_suffix}_reply"
        try:
            plan_reply = self.invoker.run_prompt(
                session_key=plan_session,
                prompt=_build_plan_prompt(event=event, history=history, category_result=category_result),
            )
            plan = _parse_plan_text(plan_reply.text, fallback_category=category_result.category)
        except Exception as exc:
            return _fallback_decision(
                category_result=category_result,
                reason=f"fallback:planner_{type(exc).__name__}",
                fallback_text=self.fallback_text,
                empty_fallback=_missing_info_reply(event),
                trace_id=trace_id,
                extra={
                    "planner_error": str(exc)[:300],
                    "openclaw_plan_session_key": plan_session,
                    "openclaw_reply_session_key": reply_session,
                },
            )

        if plan.need_tool and plan.tool_name in SUPPORTED_TOOLS:
            default_missing_reply = _missing_info_reply(event)
            if plan.missing_fields:
                ask = plan.ask_user or default_missing_reply
                safe_text = _sanitize_reply(
                    ask,
                    allow_verified_claim=False,
                    empty_fallback=default_missing_reply,
                )
                return _decision(
                    action="send",
                    reply_text=safe_text,
                    reason="need_tool_missing_fields",
                    category=category_result,
                    trace_id=trace_id,
                    need_more=True,
                    extra={
                        "plan": _plan_as_dict(plan),
                        "openclaw_plan_session_key": plan_session,
                        "openclaw_reply_session_key": reply_session,
                    },
                )

            tool_result = self.tool_router.execute(
                tool_name=plan.tool_name,
                tool_args=_normalize_tool_args(plan.tool_args),
                event=event,
            )
            if not tool_result.ok:
                safe_text = _tool_unavailable_reply(
                    event=event,
                    plan=plan,
                    tool_result=tool_result,
                    default_missing_reply=default_missing_reply,
                )
                return _decision(
                    action="send",
                    reply_text=safe_text,
                    reason=f"tool_unavailable:{tool_result.message}",
                    category=category_result,
                    trace_id=trace_id,
                    need_more=True,
                    extra={
                        "plan": _plan_as_dict(plan),
                        "tool_result": _tool_as_dict(tool_result),
                        "openclaw_plan_session_key": plan_session,
                        "openclaw_reply_session_key": reply_session,
                    },
                )

            return self._reply_with_tool(
                event=event,
                category_result=category_result,
                trace_id=trace_id,
                history=history,
                plan=plan,
                tool_result=tool_result,
                plan_session=plan_session,
                reply_session=reply_session,
            )

        return self._reply_without_tool(
            event=event,
            category_result=category_result,
            trace_id=trace_id,
            history=history,
            plan=plan,
            plan_session=plan_session,
            reply_session=reply_session,
        )

    def _acquire_session_lock(self, session_key: str) -> Lock:
        with self._locks_guard:
            lock = self._session_locks.get(session_key)
            if lock is None:
                lock = Lock()
                self._session_locks[session_key] = lock
        return lock

    def _reply_with_tool(
        self,
        *,
        event: IncomingEvent,
        category_result: CategoryResult,
        trace_id: str,
        history: list[dict[str, Any]],
        plan: PlanDecision,
        tool_result: ToolResult,
        plan_session: str,
        reply_session: str,
    ) -> DecisionInternal:
        reply_tool_result = tool_result
        reply_timeout = _pick_reply_timeout_sec(self.invoker.timeout_sec, reply_tool_result)
        try:
            reply = self.invoker.run_prompt_with_timeout(
                session_key=reply_session,
                prompt=_build_final_prompt(
                    event=event,
                    history=history,
                    plan=plan,
                    tool_result=reply_tool_result,
                ),
                timeout_sec=reply_timeout,
            ).text
        except Exception as exc:
            compact_result = _compact_tool_result_for_prompt(reply_tool_result)
            if compact_result is not None:
                try:
                    reply = self.invoker.run_prompt_with_timeout(
                        session_key=reply_session,
                        prompt=_build_final_prompt(
                            event=event,
                            history=history,
                            plan=plan,
                            tool_result=compact_result,
                        ),
                        timeout_sec=max(reply_timeout, 180.0),
                    ).text
                    reply_tool_result = compact_result
                except Exception as retry_exc:
                    return _fallback_decision(
                        category_result=category_result,
                        reason=f"fallback:reply_tool_{type(retry_exc).__name__}",
                        fallback_text=self.fallback_text,
                        empty_fallback=_missing_info_reply(event),
                        trace_id=trace_id,
                        extra={
                            "plan": _plan_as_dict(plan),
                            "tool_result": _tool_as_dict(tool_result),
                            "tool_result_compacted": _tool_as_dict(compact_result),
                            "reply_error": str(retry_exc)[:300],
                            "reply_error_first": str(exc)[:300],
                            "openclaw_plan_session_key": plan_session,
                            "openclaw_reply_session_key": reply_session,
                        },
                    )
            return _fallback_decision(
                category_result=category_result,
                reason=f"fallback:reply_tool_{type(exc).__name__}",
                fallback_text=self.fallback_text,
                empty_fallback=_missing_info_reply(event),
                trace_id=trace_id,
                extra={
                    "plan": _plan_as_dict(plan),
                    "tool_result": _tool_as_dict(tool_result),
                    "reply_error": str(exc)[:300],
                    "openclaw_plan_session_key": plan_session,
                    "openclaw_reply_session_key": reply_session,
                },
            )

        safe_text, reply_image_urls = _normalize_reply_payload(
            reply,
            event=event,
            allow_verified_claim=True,
            empty_fallback=_missing_info_reply(event),
        )
        safe_text = _stabilize_order_id_in_reply(
            text=safe_text,
            tool_name=reply_tool_result.tool_name,
            tool_data=reply_tool_result.data,
        )
        return _decision(
            action="send",
            reply_text=safe_text,
            reason="tool_reply",
            category=category_result,
            trace_id=trace_id,
            need_more=False,
            extra={
                "plan": _plan_as_dict(plan),
                "tool_result": _tool_as_dict(reply_tool_result),
                "reply_image_urls": reply_image_urls,
                "openclaw_plan_session_key": plan_session,
                "openclaw_reply_session_key": reply_session,
            },
        )

    def _reply_without_tool(
        self,
        *,
        event: IncomingEvent,
        category_result: CategoryResult,
        trace_id: str,
        history: list[dict[str, Any]],
        plan: PlanDecision,
        plan_session: str,
        reply_session: str,
    ) -> DecisionInternal:
        if plan.direct_reply.strip():
            safe_text, reply_image_urls = _normalize_reply_payload(
                plan.direct_reply,
                event=event,
                allow_verified_claim=False,
                empty_fallback=_missing_info_reply(event),
            )
            return _decision(
                action="send",
                reply_text=safe_text,
                reason="planner_direct_reply",
                category=category_result,
                trace_id=trace_id,
                need_more=bool(plan.missing_fields),
                extra={
                    "plan": _plan_as_dict(plan),
                    "reply_image_urls": reply_image_urls,
                    "openclaw_plan_session_key": plan_session,
                    "openclaw_reply_session_key": reply_session,
                },
            )

        try:
            reply = self.invoker.run_prompt(
                session_key=reply_session,
                prompt=_build_final_prompt(
                    event=event,
                    history=history,
                    plan=plan,
                    tool_result=None,
                ),
            ).text
        except Exception as exc:
            return _fallback_decision(
                category_result=category_result,
                reason=f"fallback:reply_{type(exc).__name__}",
                fallback_text=self.fallback_text,
                empty_fallback=_missing_info_reply(event),
                trace_id=trace_id,
                extra={
                    "plan": _plan_as_dict(plan),
                    "reply_error": str(exc)[:300],
                    "openclaw_plan_session_key": plan_session,
                    "openclaw_reply_session_key": reply_session,
                },
            )

        safe_text, reply_image_urls = _normalize_reply_payload(
            reply,
            event=event,
            allow_verified_claim=False,
            empty_fallback=_missing_info_reply(event),
        )
        return _decision(
            action="send",
            reply_text=safe_text,
            reason="clawbot_reply",
            category=category_result,
            trace_id=trace_id,
            need_more=bool(plan.missing_fields),
            extra={
                "plan": _plan_as_dict(plan),
                "reply_image_urls": reply_image_urls,
                "openclaw_plan_session_key": plan_session,
                "openclaw_reply_session_key": reply_session,
            },
        )


def _decision(
    *,
    action: str,
    reply_text: str,
    reason: str,
    category: CategoryResult,
    trace_id: str,
    need_more: bool,
    extra: dict[str, Any],
) -> DecisionInternal:
    payload = dict(extra)
    payload["trace_id"] = trace_id
    payload["strategy"] = "agent_two_stage"
    return DecisionInternal(
        action=action,
        reply_text=reply_text,
        reason=reason,
        category=category.category,
        confidence=category.confidence,
        amount=0.0,
        need_more=need_more,
        manual=False,
        extra=payload,
    )


def _fallback_decision(
    *,
    category_result: CategoryResult,
    reason: str,
    fallback_text: str,
    empty_fallback: str,
    trace_id: str,
    extra: dict[str, Any],
) -> DecisionInternal:
    reply = fallback_text.strip()
    if (not reply) or ("客服休息" in reply):
        # Planner/reply stage failed; avoid sending misleading "offline" wording.
        reply = empty_fallback
    safe_text = _sanitize_reply(reply, allow_verified_claim=False, empty_fallback=empty_fallback)
    payload = dict(extra)
    payload["trace_id"] = trace_id
    payload["strategy"] = "agent_two_stage"
    return DecisionInternal(
        action="send",
        reply_text=safe_text,
        reason=reason,
        category=category_result.category,
        confidence=category_result.confidence,
        amount=0.0,
        need_more=False,
        manual=False,
        extra=payload,
    )


def _build_plan_prompt(
    *,
    event: IncomingEvent,
    history: list[dict[str, Any]],
    category_result: CategoryResult,
) -> str:
    is_admin = _is_admin_event(event)
    capabilities_json = json.dumps(_channel_capabilities(event), ensure_ascii=False)
    bootstrap_json = _bootstrap_context_json(event)
    role_intro = "当前消息来自管理员（运营入口），不是顾客。\n" if is_admin else "当前消息来自顾客。\n"
    role_rules = (
        "当前不允许自动调用图片识别工具；顾客发图时，只基于文字上下文回复，必要时请顾客补充文字说明或转人工。\n"
        "如果需要工具但参数不足，设置 missing_fields 并在 ask_user 里只索要必要字段（优先订单号）。\n"
    )
    current_label = "当前管理员消息" if is_admin else "当前顾客消息"
    return (
        "你是电商客服决策器。你现在只做“是否要调用工具”的判断，必须输出JSON对象。\n"
        "禁止输出除JSON外的任何文字。\n"
        "禁止建议顾客提供手机号/电话/微信等联系方式。\n"
        f"执行通道能力(JSON): {capabilities_json}\n"
        "涉及“能否发送图片/消息”等能力问题时，必须严格依据执行通道能力回答。\n"
        f"{role_intro}"
        "可用工具仅有：order_lookup, logistics_lookup, refund_history_lookup, approve_refund_by_order_id, batch_reclass, other_actionable_review, file_read, shell_exec。\n"
        "approve_refund_by_order_id 仅用于已发货仅退款；订单实付金额小于15元时，平台实退金额最多3元且不得超过实付金额；订单实付金额大于等于15元时，平台实退金额最多5元且不得超过实付金额。\n"
        "若消息中已提供订单号，优先直接调用 logistics_lookup 或 order_lookup，不要再索要平台/店铺/昵称。\n"
        "logistics_lookup 支持 detail_level=summary|trace；默认 summary，只有顾客明确要“物流轨迹/节点明细”时才用 trace。\n"
        f"系统预取上下文(JSON): {bootstrap_json}\n"
        "若预取上下文 status=found，优先基于预取订单信息回答，不要重复索要订单号/昵称。\n"
        "若预取上下文 status=need_order_id，且当前是订单/物流/售后问题，只索要订单号。\n"
        "图片识别工具当前已禁用，不要选择 image_inspect。\n"
        f"{role_rules}"
        "direct_reply 如需附图可加 [[IMAGE_URL:https://...]] 标记。\n"
        "若 send_image=false，direct_reply 中不要输出任何 IMAGE_URL 标记。\n"
        "字段要求：\n"
        '{'
        '"intent":"quality|wrong_item|logistics|other",'
        '"confidence":0.0,'
        '"need_tool":true,'
        '"tool_name":"order_lookup|logistics_lookup|refund_history_lookup|approve_refund_by_order_id|batch_reclass|other_actionable_review|file_read|shell_exec|none",'
        '"tool_args":{},'
        '"missing_fields":["..."],'
        '"ask_user":"",'
        '"direct_reply":"",'
        '"notes":"简短理由"'
        "}\n"
        f"分类器预判: {category_result.category} ({category_result.confidence}, {category_result.reason})\n"
        f"历史对话:\n{_render_history(history, event)}\n"
        f"{current_label}: {_render_event(event)}\n"
    )


def _build_final_prompt(
    *,
    event: IncomingEvent,
    history: list[dict[str, Any]],
    plan: PlanDecision,
    tool_result: ToolResult | None,
) -> str:
    is_admin = _is_admin_event(event)
    capabilities_json = json.dumps(_channel_capabilities(event), ensure_ascii=False)
    bootstrap_json = _bootstrap_context_json(event)
    tool_block = "未调用工具。"
    if tool_result is not None:
        tool_block = (
            f"工具: {tool_result.tool_name}\n"
            f"工具状态: ok\n"
            f"工具数据: {json.dumps(tool_result.data, ensure_ascii=False)}\n"
            f"工具说明: {tool_result.message}"
        )
    role_head = (
        "你是客服运营助手，请输出给管理员的中文回复。\n"
        if is_admin
        else "你是电商店铺客服，请输出可直接发送给顾客的中文回复。\n"
    )
    role_constraints = (
        "4) 语气专业简洁，可给出排查结论和下一步，1-6句。\n"
        if is_admin
        else "4) 语气礼貌简洁，1-3句。\n"
    )
    current_label = "当前管理员消息" if is_admin else "当前顾客消息"
    return (
        f"{role_head}"
        "约束：\n"
        "1) 只输出最终回复文本，不要解释。\n"
        "2) 不要索要手机号/电话/微信/私人联系方式。\n"
        "3) 当未调用工具或工具失败时，禁止说“已查询/已核实/已催件/已查到”。\n"
        f"4) 执行通道能力(JSON): {capabilities_json}\n"
        "5) 如需附图，请在文本中使用 [[IMAGE_URL:https://...]]（可多个），不要用Markdown图片语法。\n"
        "6) 若 send_image=false，禁止输出 IMAGE_URL 标记。\n"
        f"8) 系统预取上下文(JSON): {bootstrap_json}\n"
        f"{role_constraints}"
        f"计划JSON: {json.dumps(_plan_as_dict(plan), ensure_ascii=False)}\n"
        f"工具结果:\n{tool_block}\n"
        f"历史对话:\n{_render_history(history, event)}\n"
        f"{current_label}: {_render_event(event)}\n"
    )


def _render_history(history: list[dict[str, Any]], event: IncomingEvent) -> str:
    if not history:
        return "（无）"
    is_admin = _is_admin_event(event)
    user_tag = "管理员" if is_admin else "顾客"
    bot_tag = "助手" if is_admin else "客服"
    lines: list[str] = []
    for item in history:
        customer = _clip_history_text(str(item.get("customer") or "").strip())
        agent = _clip_history_text(str(item.get("agent") or "").strip())
        if customer:
            lines.append(f"{user_tag}: {customer}")
        if agent:
            lines.append(f"{bot_tag}: {agent}")
    return "\n".join(lines) if lines else "（无）"


def _clip_history_text(text: str, limit: int = 200) -> str:
    value = (text or "").strip()
    # Keep full media URL markers, otherwise downstream image tool calls may fail.
    if value.startswith("[图片消息 URL="):
        return value
    if value.startswith("[[IMAGE_URL:"):
        return value
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _render_event(event: IncomingEvent) -> str:
    text = (event.text or "").strip()
    if event.media_url and text:
        return f"图片+文本：{text}，图片URL={event.media_url}"
    if event.media_url:
        return f"图片消息，图片URL={event.media_url}"
    return text or "（空）"


def _bootstrap_context_json(event: IncomingEvent) -> str:
    raw = event.raw if isinstance(event.raw, dict) else {}
    ctx = raw.get("bootstrap_context")
    if not isinstance(ctx, dict):
        return "{}"
    orders_raw = ctx.get("orders")
    orders = orders_raw if isinstance(orders_raw, list) else []
    compact = {
        "status": str(ctx.get("status") or ""),
        "order_id": str(ctx.get("order_id") or ""),
        "found": bool(ctx.get("found")),
        "order_count": int(ctx.get("order_count") or 0),
        "km_name": str(ctx.get("km_name") or ""),
        "identity_source": str(ctx.get("identity_source") or ""),
        "tool_message": str(ctx.get("tool_message") or ""),
        "orders": orders[:6],
    }
    return json.dumps(compact, ensure_ascii=False)


def _parse_plan_text(raw_text: str, fallback_category: str) -> PlanDecision:
    data = _extract_json(raw_text)
    if not isinstance(data, dict):
        data = {}

    tool_name = str(data.get("tool_name") or "none").strip()
    need_tool = bool(data.get("need_tool", False)) and tool_name in SUPPORTED_TOOLS
    intent = str(data.get("intent") or fallback_category or "other").strip() or "other"
    confidence = _safe_float(data.get("confidence"), default=0.5)
    tool_args = data.get("tool_args")
    if not isinstance(tool_args, dict):
        tool_args = {}
    missing_fields_raw = data.get("missing_fields")
    missing_fields: list[str] = []
    if isinstance(missing_fields_raw, list):
        for token in missing_fields_raw:
            val = str(token).strip()
            if val:
                missing_fields.append(val)

    ask_user = str(data.get("ask_user") or "").strip()
    direct_reply = str(data.get("direct_reply") or "").strip()
    notes = str(data.get("notes") or "").strip()

    if any(_has_forbidden_contact_keyword(token) for token in missing_fields):
        missing_fields = [token for token in missing_fields if not _has_forbidden_contact_keyword(token)]
    if _has_forbidden_contact_keyword(ask_user):
        ask_user = SAFE_GENERIC_MISSING_INFO_REPLY
    if _has_forbidden_contact_keyword(direct_reply):
        direct_reply = ""

    return PlanDecision(
        intent=intent,
        confidence=confidence,
        need_tool=need_tool,
        tool_name=tool_name if need_tool else "none",
        tool_args=tool_args,
        missing_fields=missing_fields,
        ask_user=ask_user,
        direct_reply=direct_reply,
        notes=notes,
    )


def _extract_json(raw_text: str) -> dict[str, Any]:
    text = (raw_text or "").strip()
    if not text:
        return {}
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            data, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return {}


def _sanitize_reply(
    text: str,
    *,
    allow_verified_claim: bool,
    empty_fallback: str = SAFE_GENERIC_MISSING_INFO_REPLY,
) -> str:
    fallback = (empty_fallback or SAFE_GENERIC_MISSING_INFO_REPLY).strip() or SAFE_GENERIC_MISSING_INFO_REPLY
    value = (text or "").strip()
    if not value:
        return fallback

    if _has_forbidden_contact_keyword(value):
        return fallback

    if (not allow_verified_claim) and _has_unverified_claim(value):
        return fallback

    # Strip accidental JSON block output.
    if value.startswith("{") and value.endswith("}"):
        return fallback
    return value


def _normalize_reply_payload(
    text: str,
    *,
    event: IncomingEvent,
    allow_verified_claim: bool,
    empty_fallback: str,
) -> tuple[str, list[str]]:
    cleaned_text, image_urls = _extract_reply_image_urls(text)
    if cleaned_text and _looks_like_internal_agent_error(cleaned_text):
        return _internal_error_reply(event), []
    if image_urls and (not _channel_can_send_image(event)):
        image_urls = []
        if not cleaned_text:
            cleaned_text = _channel_image_unsupported_reply(event)
    if cleaned_text:
        safe_text = _sanitize_reply(
            cleaned_text,
            allow_verified_claim=allow_verified_claim,
            empty_fallback=empty_fallback,
        )
        # When sanitized into fallback, drop media to avoid mixed/unsafe content.
        if safe_text != cleaned_text:
            return safe_text, []
        return safe_text, image_urls

    if image_urls:
        return "", image_urls

    if not _channel_can_send_text(event):
        return "", []

    safe_text = _sanitize_reply(
        cleaned_text,
        allow_verified_claim=allow_verified_claim,
        empty_fallback=empty_fallback,
    )
    return safe_text, []


def _extract_reply_image_urls(text: str) -> tuple[str, list[str]]:
    raw = str(text or "")
    urls: list[str] = []

    def _append(url: str) -> None:
        val = _clean_url(url)
        if not val:
            return
        if val in urls:
            return
        urls.append(val)

    def _marker_repl(match: re.Match[str]) -> str:
        _append(match.group(1))
        return ""

    def _markdown_repl(match: re.Match[str]) -> str:
        _append(match.group(1))
        return ""

    text_wo_markers = IMAGE_URL_MARKER_RE.sub(_marker_repl, raw)
    text_wo_markers = MARKDOWN_IMAGE_RE.sub(_markdown_repl, text_wo_markers)
    text_wo_markers = re.sub(r"\n{3,}", "\n\n", text_wo_markers)
    return text_wo_markers.strip(), urls


def _clean_url(raw: str) -> str:
    val = str(raw or "").strip().strip("\"'<>")
    while val and val[-1] in ".,;)]}，。；）】":
        val = val[:-1].rstrip()
    if not val:
        return ""
    if not re.match(r"^https?://", val, flags=re.IGNORECASE):
        return ""
    return val


def _channel_capabilities(event: IncomingEvent) -> dict[str, Any]:
    raw = event.raw if isinstance(event.raw, dict) else {}
    caps = raw.get("channel_capabilities")
    if not isinstance(caps, dict):
        return {}
    return dict(caps)


def _channel_can_send_image(event: IncomingEvent) -> bool:
    caps = _channel_capabilities(event)
    val = caps.get("send_image")
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() not in {"", "0", "false", "no", "off"}
    return False


def _channel_can_send_text(event: IncomingEvent) -> bool:
    caps = _channel_capabilities(event)
    val = caps.get("send_text")
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() not in {"", "0", "false", "no", "off"}
    return True


def _channel_image_unsupported_reply(event: IncomingEvent) -> str:
    return "当前通道暂不支持图片回复，我先用文字为您处理。"


def _is_admin_event(event: IncomingEvent) -> bool:
    return event.role_key() == "admin"


def _missing_info_reply(event: IncomingEvent) -> str:
    if _is_admin_event(event):
        return SAFE_ADMIN_MISSING_INFO_REPLY
    return SAFE_CUSTOMER_MISSING_INFO_REPLY


def _tool_unavailable_reply(
    *,
    event: IncomingEvent,
    plan: PlanDecision,
    tool_result: ToolResult,
    default_missing_reply: str,
) -> str:
    ask = (plan.ask_user or "").strip()
    mapped = _map_tool_error_reply(
        event=event,
        tool_name=plan.tool_name,
        tool_message=str(tool_result.message or ""),
    )
    candidate = mapped or ask or default_missing_reply
    return _sanitize_reply(
        candidate,
        allow_verified_claim=False,
        empty_fallback=default_missing_reply,
    )


def _map_tool_error_reply(*, event: IncomingEvent, tool_name: str, tool_message: str) -> str:
    msg = (tool_message or "").strip().lower()

    if "identity_not_found" in msg:
        return "暂时没定位到相关订单，请发订单号，我继续查。"

    if "identity_ambiguous" in msg:
        return "命中多条相近记录，请发订单号，我继续查。"

    if "missing_order_id" in msg:
        return "请发一下订单号，我马上继续查询。"

    if "vision_call_failed" in msg:
        return "图片识别失败，请稍后重试，或换一张更清晰的图片。"

    if "missing_image_url" in msg:
        return "图片链接读取失败，请重新发送一张清晰图片。"

    if "kuaimai_login_failed" in msg:
        return "快麦登录态失效，暂时无法查询订单/物流，请稍后再试。"

    if "trade_search_timeout" in msg:
        return "查询超时了，请稍后再试。"

    if tool_name == "image_inspect" and (not msg):
        return "图片工具暂不可用，请稍后重试。"

    return ""


def _has_forbidden_contact_keyword(text: str) -> bool:
    lowered = (text or "").lower()
    return any(token.lower() in lowered for token in FORBIDDEN_CONTACT_KEYWORDS)


def _has_unverified_claim(text: str) -> bool:
    value = text or ""
    return any(token in value for token in UNVERIFIED_CLAIM_KEYWORDS)


def _looks_like_internal_agent_error(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return False
    patterns = (
        r"Traceback \(most recent call last\):",
        r"Command exited with code\s+\d+",
        r"^\s*⚠️?\s*🛠️?\s*Exec:",
        r"\"tool\"\s*:\s*\"exec\"",
        r"\"status\"\s*:\s*\"error\"",
        r"/bin/bash:",
        r"OperationalError:",
        r"RuntimeError:",
    )
    return any(re.search(p, value, flags=re.IGNORECASE | re.MULTILINE) for p in patterns)


def _internal_error_reply(event: IncomingEvent) -> str:
    return "稍等，我这边继续核实，马上回复。"


def _pick_reply_timeout_sec(base_timeout_sec: float, tool_result: ToolResult) -> float:
    timeout = max(10.0, float(base_timeout_sec or 60.0))
    if tool_result.tool_name not in {"file_read", "shell_exec"}:
        return timeout
    data = tool_result.data if isinstance(tool_result.data, dict) else {}
    size_hint = 0
    content = data.get("content")
    if isinstance(content, str):
        size_hint = max(size_hint, len(content))
    stdout = data.get("stdout")
    if isinstance(stdout, str):
        size_hint = max(size_hint, len(stdout))
    if size_hint >= 20000:
        return max(timeout, 240.0)
    if size_hint >= 8000:
        return max(timeout, 150.0)
    return max(timeout, 90.0)


def _is_openclaw_empty_reply_error(exc: Exception) -> bool:
    return "openclaw_empty_reply" in str(exc or "").lower()


def _compact_tool_result_for_prompt(result: ToolResult) -> ToolResult | None:
    if result.tool_name not in {"file_read", "shell_exec"}:
        return None
    if not isinstance(result.data, dict):
        return None

    compact_data = _compact_json_for_prompt(result.data)
    if compact_data == result.data:
        return None

    return ToolResult(
        ok=result.ok,
        tool_name=result.tool_name,
        data=compact_data,
        message=result.message,
        source=result.source,
    )


def _compact_json_for_prompt(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return "[trimmed]"

    if isinstance(value, str):
        if len(value) <= 12000:
            return value
        head = value[:8000]
        tail = value[-2000:]
        return f"{head}\n...[trimmed {len(value) - 10000} chars]...\n{tail}"

    if isinstance(value, list):
        if len(value) <= 80:
            return [_compact_json_for_prompt(v, depth=depth + 1) for v in value]
        head = [_compact_json_for_prompt(v, depth=depth + 1) for v in value[:40]]
        tail = [_compact_json_for_prompt(v, depth=depth + 1) for v in value[-10:]]
        return head + [f"...[trimmed {len(value) - 50} items]..."] + tail

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        keys = list(value.keys())
        for k in keys[:80]:
            out[str(k)] = _compact_json_for_prompt(value[k], depth=depth + 1)
        if len(keys) > 80:
            out["__trimmed_keys__"] = len(keys) - 80
        return out

    return value


def _safe_float(raw: Any, default: float) -> float:
    try:
        value = float(raw)
    except Exception:
        return default
    if value < 0:
        return 0.0
    if value > 1:
        return 1.0
    return value


def _plan_as_dict(plan: PlanDecision) -> dict[str, Any]:
    return {
        "intent": plan.intent,
        "confidence": plan.confidence,
        "need_tool": plan.need_tool,
        "tool_name": plan.tool_name,
        "tool_args": plan.tool_args,
        "missing_fields": plan.missing_fields,
        "ask_user": plan.ask_user,
        "direct_reply": plan.direct_reply,
        "notes": plan.notes,
    }


def _tool_as_dict(result: ToolResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "tool_name": result.tool_name,
        "message": result.message,
        "source": result.source,
        "data": result.data,
    }


def _normalize_tool_args(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}

    out: dict[str, Any] = dict(raw)
    nested_args = out.get("args")
    if isinstance(nested_args, dict):
        for key, val in nested_args.items():
            out.setdefault(str(key), val)
        out.pop("args", None)

    nested_event = out.get("event")
    if isinstance(nested_event, dict):
        # Common planner mistake: nest useful args under event.
        alias_map = (
            ("order_id", "order_id"),
            ("platform", "platform"),
            ("store_name", "shop_name"),
            ("platform_nickname", "platform_nickname"),
            ("km_name", "km_name"),
        )
        for src, dst in alias_map:
            if (dst not in out) or (not str(out.get(dst) or "").strip()):
                val = str(nested_event.get(src) or "").strip()
                if val:
                    out[dst] = val
        out.pop("event", None)

    return out


def _stabilize_order_id_in_reply(*, text: str, tool_name: str, tool_data: Any) -> str:
    if tool_name != "order_lookup":
        return text
    if not isinstance(tool_data, dict):
        return text

    canonical = ""
    data_order = str(tool_data.get("order_id") or "").strip()
    if data_order:
        canonical = data_order
    else:
        orders = tool_data.get("orders")
        if isinstance(orders, list) and orders:
            first = orders[0] if isinstance(orders[0], dict) else {}
            canonical = str(first.get("order_id") or "").strip()

    if not canonical:
        return text

    # Prevent model from mutating one digit in long order ids.
    value = str(text or "")
    tokens = re.findall(r"\b\d{12,}\b", value)
    if not tokens:
        return value
    for token in set(tokens):
        if token == canonical:
            continue
        value = value.replace(token, canonical)
    return value
