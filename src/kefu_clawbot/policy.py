from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from .models import CategoryResult, DecisionInternal, IncomingEvent

DEFAULT_POLICY: dict[str, Any] = {
    "manual": {
        "keywords": [
            "转人工",
            "投诉",
            "差评",
            "平台介入",
            "12315",
            "工商",
            "法院",
            "报警",
            "律师",
        ]
    },
    "categories": {
        "logistics": {
            "reply": "亲，已收到您的物流问题，我这边马上帮您催件并核对进度，请稍等。",
            "need_more": False,
            "amount": 0,
        },
        "wrong_item": {
            "reply": "亲，已收到您反馈的发错货问题。请发订单号、商品吊牌照片和实物全图；信息齐全后这边才能核对并确认处理方案。",
            "need_more": True,
            "amount": 0,
        },
        "quality": {
            "reply": "亲，已收到您反馈的质量问题。请发订单号和问题细节近照（至少2张）；信息齐全后这边才能核对并确认处理方案。",
            "need_more": True,
            "amount": 0,
        },
        "unknown_image": {
            "reply": "亲，图片已收到。请再补充订单号和问题描述（质量问题/发错货/物流问题）；信息齐全后这边才能核对处理。",
            "need_more": True,
            "amount": 0,
        },
        "other": {
            "reply": "亲，已收到您的消息，我这边正在核实处理，请稍等。",
            "need_more": False,
            "amount": 0,
        },
    },
}


class PolicyEngine:
    def __init__(self, policy: dict[str, Any]) -> None:
        self.policy = policy

    @classmethod
    def from_file(cls, path: str | Path | None) -> "PolicyEngine":
        merged = copy.deepcopy(DEFAULT_POLICY)
        if not path:
            return cls(merged)

        file_path = Path(path)
        if not file_path.exists():
            return cls(merged)

        with file_path.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}

        if isinstance(loaded, dict):
            _deep_merge_dict(merged, loaded)
        return cls(merged)

    def decide(
        self,
        event: IncomingEvent,
        category_result: CategoryResult,
        trace_id: str,
    ) -> DecisionInternal:
        text = (event.text or "").strip()
        manual_keywords = tuple(self.policy.get("manual", {}).get("keywords", []))
        if text and any(token in text for token in manual_keywords):
            return DecisionInternal(
                action="manual",
                reply_text="",
                reason="manual_keyword",
                category=category_result.category,
                confidence=category_result.confidence,
                amount=0.0,
                need_more=False,
                manual=True,
                extra={
                    "trace_id": trace_id,
                    "category_reason": category_result.reason,
                },
            )

        category_cfg = self.policy.get("categories", {}).get(
            category_result.category,
            self.policy.get("categories", {}).get("other", {}),
        )

        reply_text = str(category_cfg.get("reply") or "")
        need_more = bool(category_cfg.get("need_more", False))
        amount = float(category_cfg.get("amount", 0))

        # Conservative path: when uncertainty is high, ask for more evidence.
        if category_result.confidence < 0.5 and category_result.category in {
            "quality",
            "wrong_item",
        }:
            need_more = True

        reason = f"{category_result.category}|{category_result.reason}"
        return DecisionInternal(
            action="send",
            reply_text=reply_text,
            reason=reason,
            category=category_result.category,
            confidence=category_result.confidence,
            amount=amount,
            need_more=need_more,
            manual=False,
            extra={
                "trace_id": trace_id,
                "category_reason": category_result.reason,
                "strategy": "douyin_v1",
            },
        )

    def manual_keywords(self) -> tuple[str, ...]:
        tokens = self.policy.get("manual", {}).get("keywords", [])
        return tuple(str(token) for token in tokens if str(token).strip())

    def is_manual(self, text: str) -> bool:
        value = (text or "").strip()
        if not value:
            return False
        return any(token in value for token in self.manual_keywords())


def _deep_merge_dict(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _deep_merge_dict(dst[key], value)
        else:
            dst[key] = value
