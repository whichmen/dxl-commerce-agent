from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain import PolicyDecision, PolicyOutcome


@dataclass(frozen=True, slots=True)
class RefundPolicy:
    auto_approve_limit_cents: int
    maximum_refund_cents: int
    require_evidence_for: frozenset[str]

    @classmethod
    def from_file(cls, path: Path) -> RefundPolicy:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)["refunds"]
        return cls(
            auto_approve_limit_cents=int(raw["auto_approve_limit_cents"]),
            maximum_refund_cents=int(raw["maximum_refund_cents"]),
            require_evidence_for=frozenset(raw["require_evidence_for"]),
        )


class PolicyEngine:
    def __init__(self, refund_policy: RefundPolicy) -> None:
        self.refund_policy = refund_policy

    def requires_evidence(self, reason: str) -> bool:
        return reason in self.refund_policy.require_evidence_for

    def evaluate_refund(
        self,
        *,
        order: dict[str, Any],
        amount_cents: int,
        reason: str,
        has_evidence: bool,
    ) -> PolicyDecision:
        if order.get("refund_status") in {"pending", "succeeded"}:
            return PolicyDecision(
                PolicyOutcome.DENY,
                "REFUND_ALREADY_EXISTS",
                "The synthetic order already has an active or completed refund.",
            )
        if amount_cents <= 0:
            return PolicyDecision(
                PolicyOutcome.DENY,
                "INVALID_AMOUNT",
                "Refund amount must be positive.",
            )
        if amount_cents > int(order["paid_amount_cents"]):
            return PolicyDecision(
                PolicyOutcome.DENY,
                "ABOVE_PAID_AMOUNT",
                "Refund amount cannot exceed the verified paid amount.",
            )
        if amount_cents > self.refund_policy.maximum_refund_cents:
            return PolicyDecision(
                PolicyOutcome.DENY,
                "ABOVE_POLICY_MAXIMUM",
                "Refund amount exceeds the sandbox policy maximum.",
            )
        if self.requires_evidence(reason) and not has_evidence:
            return PolicyDecision(
                PolicyOutcome.DENY,
                "EVIDENCE_REQUIRED",
                "This reason requires an attachment before an action can be proposed.",
            )
        if amount_cents > self.refund_policy.auto_approve_limit_cents:
            return PolicyDecision(
                PolicyOutcome.REQUIRE_APPROVAL,
                "HUMAN_APPROVAL_REQUIRED",
                "Amount exceeds the automatic sandbox approval limit.",
            )
        return PolicyDecision(
            PolicyOutcome.ALLOW,
            "WITHIN_AUTO_LIMIT",
            "Action is within the sandbox limit and may be executed idempotently.",
        )
