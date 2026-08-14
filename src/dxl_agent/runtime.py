from __future__ import annotations

import asyncio
import hashlib
import re
import time
import uuid
from collections import defaultdict
from copy import deepcopy
from dataclasses import replace
from typing import Any

from .config import Settings
from .domain import (
    ActionState,
    IncomingMessage,
    Intent,
    PolicyOutcome,
    TraceStep,
    scoped_key,
)
from .fixtures import SyntheticFixtures
from .planner import (
    OpenAICompatiblePlanner,
    Planner,
    RuleBasedPlanner,
    classify_explicit_refund_reason,
    extract_order_ids,
    has_explicit_refund_request,
    has_refund_term,
    is_full_refund_request,
    parse_refund_amount_cents,
)
from .policy import PolicyEngine, RefundPolicy
from .storage import SQLiteStore
from .tools import CommerceTools

_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_TOKEN_RE = re.compile(r"(?i)(bearer\s+|api[_-]?key[=:]\s*)[A-Za-z0-9._-]{8,}")


def redact(text: str) -> str:
    text = _PHONE_RE.sub("[REDACTED_PHONE]", text)
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    return _TOKEN_RE.sub("[REDACTED_SECRET]", text)


class SessionLocks:
    def __init__(self) -> None:
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def get(self, session_key: str) -> asyncio.Lock:
        return self._locks[session_key]


class AgentRuntime:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        tools: CommerceTools,
        policy: PolicyEngine,
        planner: Planner,
        max_history_turns: int = 8,
        demo_mode: bool = True,
        planner_mode: str = "rules",
        operator_key: str | None = None,
    ) -> None:
        if not demo_mode:
            raise ValueError("The built-in runtime currently supports local sandbox mode only")
        self.store = store
        self.tools = tools
        self.policy = policy
        self.planner = planner
        self.max_history_turns = max_history_turns
        self.demo_mode = demo_mode
        self.planner_mode = planner_mode
        self.operator_key = operator_key
        self.session_locks = SessionLocks()

    @classmethod
    def from_settings(cls, settings: Settings) -> AgentRuntime:
        fixtures = SyntheticFixtures(settings.fixture_dir)
        planner: Planner
        if settings.planner_mode == "openai-compatible":
            if not settings.llm_base_url or not settings.llm_model:
                raise ValueError(
                    "DXL_LLM_BASE_URL and DXL_LLM_MODEL are required for openai-compatible mode"
                )
            planner = OpenAICompatiblePlanner(
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                api_key=settings.llm_api_key,
            )
        elif settings.planner_mode == "rules":
            planner = RuleBasedPlanner()
        else:
            raise ValueError(f"Unsupported planner mode: {settings.planner_mode}")
        return cls(
            store=SQLiteStore(settings.database_path),
            tools=CommerceTools(fixtures),
            policy=PolicyEngine(RefundPolicy.from_file(settings.policy_path)),
            planner=planner,
            max_history_turns=settings.max_history_turns,
            demo_mode=settings.demo_mode,
            planner_mode=settings.planner_mode,
            operator_key=settings.operator_key,
        )

    async def handle_message(self, message: IncomingMessage) -> dict[str, Any]:
        async with self.session_locks.get(message.session_key):
            claim_token, previous = self.store.claim_message(message.dedupe_key)
            if previous is not None:
                result = deepcopy(previous)
                result["deduplicated"] = True
                return result
            if claim_token is None:
                raise RuntimeError("Message claim returned neither ownership nor a response")
            try:
                return await self._handle_claimed_message(message, claim_token)
            except Exception:
                self.store.release_message_claim(message.dedupe_key, claim_token)
                raise

    async def _handle_claimed_message(
        self, message: IncomingMessage, claim_token: str
    ) -> dict[str, Any]:
        trace_id = f"trace_{uuid.uuid4().hex}"
        public_session = hashlib.sha256(message.session_key.encode()).hexdigest()[:16]
        steps: list[TraceStep] = [
            TraceStep("runtime", "message_received", "ok"),
            TraceStep("runtime", "dedup_check", "new"),
        ]
        history = self.store.recent_turns(message.session_key, self.max_history_turns)

        started = time.perf_counter()
        planner_message = replace(message, text=redact(message.text))
        plan = await self.planner.plan(planner_message, history)
        steps.append(
            TraceStep(
                "planner",
                "intent_classification",
                "ok",
                {"intent": plan.intent.value},
                round((time.perf_counter() - started) * 1000),
            )
        )

        response = await self._execute_plan(message, plan, trace_id, public_session, steps)
        trace = {
            "trace_id": trace_id,
            "session_key": public_session,
            "intent": plan.intent.value,
            "status": response["status"],
            "steps": [step.to_dict() for step in steps],
            "facts_used": response["facts_used"],
            "demo_mode": True,
        }
        self.store.finalize_message(
            dedupe_key=message.dedupe_key,
            claim_token=claim_token,
            session_key=message.session_key,
            user_content=redact(message.text)[:2000],
            assistant_content=response["reply"][:2000],
            trace_id=trace_id,
            trace=trace,
            response=response,
        )
        return response

    async def _execute_plan(
        self,
        message: IncomingMessage,
        plan: Any,
        trace_id: str,
        public_session: str,
        steps: list[TraceStep],
    ) -> dict[str, Any]:
        base: dict[str, Any] = {
            "trace_id": trace_id,
            "session_key": public_session,
            "intent": plan.intent.value,
            "status": "resolved",
            "reply": "",
            "tool_calls": [],
            "policy": None,
            "pending_action": None,
            "facts_used": [],
            "deduplicated": False,
            "demo_mode": True,
        }

        if plan.intent == Intent.SECURITY_REJECTED:
            steps.append(TraceStep("security", "prompt_boundary", "rejected", {"reason": "marker"}))
            base.update(
                status="rejected",
                reply="我不能更改系统规则或披露内部提示。可以继续协助订单、物流或售后问题。",
            )
            return base

        if plan.intent == Intent.GREETING:
            base["reply"] = "您好，我可以协助查询演示订单、物流、商品资料和售后流程。"
            return base
        if plan.intent == Intent.OUT_OF_SCOPE:
            base.update(
                status="out_of_scope",
                reply="这个问题不在演示客服的业务范围内。我可以协助订单、物流、商品和售后问题。",
            )
            return base
        if plan.intent == Intent.UNKNOWN:
            base.update(
                status="needs_clarification",
                reply="请补充您要咨询的是订单、物流、商品资料还是售后问题。",
            )
            return base
        if plan.intent == Intent.REFUND_INQUIRY:
            base["reply"] = (
                "演示规则会校验订单归属、已付金额、售后原因和证据；"
                "超过自动限额的动作必须人工审批。若要申请，请明确订单号和金额。"
            )
            return base
        if plan.intent == Intent.REFUND_REQUEST and not has_explicit_refund_request(message.text):
            steps.append(
                TraceStep(
                    "security",
                    "explicit_action_gate",
                    "rejected",
                    {"reason": "no_explicit_refund_request"},
                )
            )
            if has_refund_term(message.text):
                base.update(
                    status="needs_clarification",
                    reply="请明确是咨询退款规则，还是要对指定订单申请退款，并提供金额。",
                )
            else:
                base.update(
                    status="rejected",
                    reply="没有检测到顾客明确提出退款，因此不会创建任何退款动作。",
                )
            return base
        if plan.intent == Intent.REFUND_REQUEST:
            parsed_amount, amount_error = parse_refund_amount_cents(message.text)
            if amount_error is not None:
                base.update(
                    status="needs_clarification",
                    reply="退款金额格式不明确；请只提供一个金额，最多保留两位小数。",
                )
                return base
            if parsed_amount is None and not is_full_refund_request(message.text):
                base.update(
                    status="needs_clarification",
                    reply="请提供明确退款金额；如需退回全部已付金额，请明确写“请全额退款”。",
                )
                return base
            refund_order_ids = extract_order_ids(message.text)
            if len(refund_order_ids) != 1:
                steps.append(
                    TraceStep(
                        "security",
                        "refund_order_gate",
                        "rejected",
                        {"reason": "explicit_single_order_required"},
                    )
                )
                base.update(
                    status="needs_clarification",
                    reply="退款动作必须由顾客明确提供一个演示订单号，请补充后重试。",
                )
                return base
            requested_order_id = refund_order_ids[0]
            steps.append(
                TraceStep(
                    "security",
                    "refund_order_gate",
                    "ok",
                    {"source": "customer_text"},
                )
            )
        else:
            requested_order_id = plan.order_id
        if plan.intent == Intent.PRODUCT_QUESTION:
            products = self._call_tool(
                steps,
                base,
                "product_lookup",
                {"sku": plan.sku},
                self.tools.product_lookup,
                tenant_id=message.tenant_id,
                store_id=message.store_id,
                sku=plan.sku,
            )
            if not products:
                base.update(
                    status="needs_clarification",
                    reply="请提供演示商品SKU；没有权威商品资料时我不会猜测。",
                )
                return base
            product = products[0]
            base["reply"] = (
                f"{product['name']}：材质为{product['material']}；护理建议为"
                f"{product['care']}；演示库存{product['stock']}件，价格"
                f"¥{product['price_cents'] / 100:.2f}。"
            )
            base["facts_used"] = [
                "product.name",
                "product.material",
                "product.care",
                "product.stock",
                "product.price_cents",
            ]
            return base

        orders = self._call_tool(
            steps,
            base,
            "order_lookup",
            {"order_id": requested_order_id},
            self.tools.order_lookup,
            tenant_id=message.tenant_id,
            store_id=message.store_id,
            customer_id=message.customer_id,
            order_id=requested_order_id,
        )
        if not orders:
            base.update(
                status="not_found",
                reply="在当前顾客和店铺范围内没有找到对应演示订单，请核对订单号。",
            )
            return base
        if len(orders) > 1 and not plan.order_id:
            base.update(
                status="needs_clarification",
                reply="当前顾客有多个演示订单，请提供具体订单号后再查询。",
            )
            return base
        order = orders[0]

        if plan.intent == Intent.ORDER_STATUS:
            base["reply"] = f"演示订单{order['order_id']}当前状态为：{order['status']}。"
            base["facts_used"] = ["order.order_id", "order.status"]
            return base

        if plan.intent == Intent.LOGISTICS_STATUS:
            shipment = self._call_tool(
                steps,
                base,
                "logistics_lookup",
                {"order_id": order["order_id"]},
                self.tools.logistics_lookup,
                order_id=order["order_id"],
            )
            if not shipment:
                base.update(
                    status="not_available",
                    reply=f"演示订单{order['order_id']}暂时没有可用物流轨迹。",
                )
                base["facts_used"] = ["order.order_id"]
                return base
            base["reply"] = (
                f"演示订单{order['order_id']}的物流状态为{shipment['status']}；"
                f"最新节点：{shipment['latest_event']}（{shipment['updated_at']}）。"
            )
            base["facts_used"] = [
                "order.order_id",
                "shipment.status",
                "shipment.latest_event",
                "shipment.updated_at",
            ]
            return base

        parsed_amount, _ = parse_refund_amount_cents(message.text)
        amount_cents = int(order["paid_amount_cents"]) if parsed_amount is None else parsed_amount
        reason = classify_explicit_refund_reason(message.text)
        evidence: list[dict[str, Any]] = []
        if self.policy.requires_evidence(reason) and message.attachments:
            evidence = self._call_tool(
                steps,
                base,
                "evidence_lookup",
                {"evidence_ids": list(message.attachments)},
                self.tools.evidence_lookup,
                tenant_id=message.tenant_id,
                store_id=message.store_id,
                customer_id=message.customer_id,
                order_id=order["order_id"],
                evidence_ids=message.attachments,
            )
        decision = self.policy.evaluate_refund(
            order=order,
            amount_cents=amount_cents,
            reason=reason,
            has_evidence=bool(evidence),
        )
        base["policy"] = decision.to_dict()
        steps.append(
            TraceStep(
                "policy",
                "refund_policy",
                decision.outcome.value,
                {"reason_code": decision.reason_code},
            )
        )
        if decision.outcome == PolicyOutcome.DENY:
            base.update(
                status="denied",
                reply=f"当前无法创建退款动作：{decision.explanation}",
            )
            return base

        action_id = f"action_{uuid.uuid4().hex}"
        state = (
            ActionState.PENDING_APPROVAL
            if decision.outcome == PolicyOutcome.REQUIRE_APPROVAL
            else ActionState.APPROVED
        )
        payload = {
            "tenant_id": message.tenant_id,
            "order_id": order["order_id"],
            "store_id": message.store_id,
            "customer_id": message.customer_id,
            "amount_cents": amount_cents,
            "reason": reason,
            "evidence_ids": [item["evidence_id"] for item in evidence],
            "sandbox": True,
        }
        business_key = scoped_key(
            "refund",
            message.tenant_id,
            message.store_id,
            message.customer_id,
            order["order_id"],
        )
        action, existing = self.store.create_action(
            action_id, business_key, payload, decision.to_dict(), state
        )
        action_id = str(action["action_id"])
        state = ActionState(str(action["state"]))
        # A business-key collision means this order already has a refund workflow.
        # Report the persisted action, never the amount or policy from the retry.
        if existing:
            amount_cents = int(action["payload"]["amount_cents"])
            base["policy"] = action["policy"]
        steps.append(
            TraceStep(
                "action",
                "refund_proposal",
                state.value,
                {"deduplicated": existing},
            )
        )
        base["pending_action"] = {
            "action_id": action_id,
            "type": "refund",
            "state": state.value,
            "amount_cents": amount_cents,
            "sandbox": True,
            "deduplicated": existing,
        }
        base["facts_used"] = ["order.order_id", "order.paid_amount_cents"]
        if evidence:
            base["facts_used"].append("evidence.evidence_id")
        if state == ActionState.PENDING_APPROVAL:
            base.update(
                status="pending_approval",
                reply=(
                    f"{'已有' if existing else '已基于演示订单创建'}"
                    f"¥{amount_cents / 100:.2f}退款提案；"
                    "金额超过自动限额，需要人工审批后才能执行。"
                ),
            )
        elif state == ActionState.APPROVED:
            base.update(
                status="action_ready",
                reply=(
                    f"{'已有' if existing else '已创建'}¥{amount_cents / 100:.2f}"
                    "的沙箱退款动作；"
                    "请通过执行接口并提供幂等键完成演示操作。"
                ),
            )
        else:
            base.update(
                status="already_completed",
                reply="该演示订单的退款动作已经执行完成，不会重复执行。",
            )
        return base

    @staticmethod
    def _call_tool(
        steps: list[TraceStep],
        response: dict[str, Any],
        name: str,
        public_args: dict[str, Any],
        function: Any,
        **kwargs: Any,
    ) -> Any:
        started = time.perf_counter()
        result = function(**(kwargs or public_args))
        latency_ms = round((time.perf_counter() - started) * 1000)
        call = {"name": name, "status": "ok", "latency_ms": latency_ms, "args": public_args}
        response["tool_calls"].append(call)
        steps.append(TraceStep("tool", name, "ok", public_args, latency_ms))
        return result

    def approve_action(self, action_id: str, approver: str) -> dict[str, Any]:
        return self.store.approve_action(action_id, approver)

    def execute_action(self, action_id: str, idempotency_key: str) -> dict[str, Any]:
        return self.store.execute_action(action_id, idempotency_key)

    def get_trace(self, trace_id: str) -> dict[str, Any]:
        return self.store.get_trace(trace_id)

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok" if self.store.health() else "degraded",
            "demo_mode": self.demo_mode,
            "planner_mode": self.planner_mode,
        }
