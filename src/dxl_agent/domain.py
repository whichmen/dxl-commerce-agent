from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def scoped_key(namespace: str, *parts: str) -> str:
    """Hash a canonical tuple so user-controlled delimiters cannot collide."""

    canonical = json.dumps(
        [namespace, *parts],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return f"{namespace}_{hashlib.sha256(canonical).hexdigest()}"


class Intent(StrEnum):
    LOGISTICS_STATUS = "logistics_status"
    ORDER_STATUS = "order_status"
    PRODUCT_QUESTION = "product_question"
    REFUND_INQUIRY = "refund_inquiry"
    REFUND_REQUEST = "refund_request"
    GREETING = "greeting"
    OUT_OF_SCOPE = "out_of_scope"
    SECURITY_REJECTED = "security_rejected"
    UNKNOWN = "unknown"


class PolicyOutcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class ActionState(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    DENIED = "denied"
    SUCCEEDED = "succeeded"


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    tenant_id: str
    channel: str
    store_id: str
    customer_id: str
    message_id: str
    text: str
    attachments: tuple[str, ...] = ()
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def session_key(self) -> str:
        return scoped_key(
            "session",
            self.tenant_id,
            self.channel,
            self.store_id,
            self.customer_id,
        )

    @property
    def dedupe_key(self) -> str:
        return scoped_key(
            "message",
            self.tenant_id,
            self.channel,
            self.store_id,
            self.customer_id,
            self.message_id,
        )


@dataclass(frozen=True, slots=True)
class DecisionPlan:
    intent: Intent
    order_id: str | None = None
    sku: str | None = None
    refund_amount_cents: int | None = None
    refund_reason: str | None = None
    needs_evidence: bool = False
    security_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    outcome: PolicyOutcome
    reason_code: str
    explanation: str

    def to_dict(self) -> dict[str, str]:
        return {
            "outcome": self.outcome.value,
            "reason_code": self.reason_code,
            "explanation": self.explanation,
        }


@dataclass(slots=True)
class TraceStep:
    kind: str
    name: str
    status: str
    detail: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AgentError(RuntimeError):
    """Base error for safe API translation."""


class NotFoundError(AgentError):
    pass


class ConflictError(AgentError):
    pass


class InvalidTransitionError(AgentError):
    pass
