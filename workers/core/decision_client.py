"""HTTP client for Ubuntu decision service."""

from __future__ import annotations

import asyncio
import json
import socket
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from .decision_alert import build_notifier_for_message
from .models import DecisionResult, IncomingMessage


class DecisionClient:
    """Simple sync-over-async HTTP caller with local fallback."""

    INVALID_REPLY_FALLBACK_TEXT = "收到，客服不在，她回来帮您核实，请稍等。"
    INVALID_REPLY_MIN_LEN = 20
    INVALID_REPLY_MIN_ALPHA = 12

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout_sec: float = 6.0,
        fallback_text: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.timeout_sec = timeout_sec
        self.fallback_text = fallback_text
        self._alert_notifier = None

    async def decide(self, msg: IncomingMessage) -> DecisionResult:
        payload = {"event": msg.as_payload()}
        if self._alert_notifier is None:
            self._alert_notifier = build_notifier_for_message(
                platform=msg.platform,
                store_id=msg.store_id,
                store_name=msg.store_name,
            )

        try:
            data = await asyncio.to_thread(self._post_json, "/v1/decide", payload)
            result = DecisionResult.from_dict(data)
            if result.action not in {"send", "skip", "manual"}:
                return DecisionResult(
                    action="skip",
                    reply_text="",
                    reason=f"invalid_action:{result.action}",
                    extra={"raw": data},
                )
            if result.action == "send" and self._looks_like_invalid_reply_text(result.reply_text):
                notifier = self._alert_notifier
                if notifier is not None:
                    notifier.record_failure(
                        detail=f"invalid_reply_text: {result.reply_text}",
                        session_key=msg.session_key(),
                        platform_nickname=msg.platform_nickname,
                        message_id=msg.message_id,
                        message_type=msg.message_type,
                    )
                return DecisionResult(
                    action="send",
                    reply_text=self.INVALID_REPLY_FALLBACK_TEXT,
                    reason="fallback:invalid_reply_text",
                    extra={"raw": data, "invalid_reply_text": str(result.reply_text or "").strip()},
                )
            notifier = self._alert_notifier
            if notifier is not None:
                notifier.record_recovery(detail="decision_api_ok")
            return result
        except Exception as exc:
            notifier = self._alert_notifier
            if notifier is not None:
                notifier.record_failure(
                    detail=f"{type(exc).__name__}: {exc}",
                    session_key=msg.session_key(),
                    platform_nickname=msg.platform_nickname,
                    message_id=msg.message_id,
                    message_type=msg.message_type,
                )
            # Timeout/network/5xx fallback
            if self.fallback_text:
                return DecisionResult(
                    action="send",
                    reply_text=self.fallback_text,
                    reason=f"fallback:{type(exc).__name__}",
                    extra={"error": str(exc)},
                )
            return DecisionResult(
                action="skip",
                reply_text="",
                reason=f"request_failed:{type(exc).__name__}",
                extra={"error": str(exc)},
            )

    @classmethod
    def _looks_like_invalid_reply_text(cls, text: str) -> bool:
        value = " ".join(str(text or "").split())
        if len(value) < cls.INVALID_REPLY_MIN_LEN:
            return False
        if any(cls._is_cjk_char(ch) for ch in value):
            return False
        alpha_count = sum(1 for ch in value if ("a" <= ch <= "z") or ("A" <= ch <= "Z"))
        return alpha_count >= cls.INVALID_REPLY_MIN_ALPHA

    @staticmethod
    def _is_cjk_char(ch: str) -> bool:
        code = ord(ch)
        return (
            0x3400 <= code <= 0x4DBF
            or 0x4E00 <= code <= 0x9FFF
            or 0xF900 <= code <= 0xFAFF
        )

    async def ack_delivery(
        self,
        *,
        msg: IncomingMessage,
        decision: DecisionResult,
        sent_text: str,
        sent_image_urls: list[str],
        status: str = "sent",
    ) -> bool:
        payload = {
            "event": msg.as_payload(),
            "trace_id": str((decision.extra or {}).get("trace_id") or "").strip(),
            "status": str(status or "sent").strip() or "sent",
            "reason": str(decision.reason or "").strip(),
            "sent_text": str(sent_text or "").strip(),
            "sent_image_urls": [str(x or "").strip() for x in (sent_image_urls or []) if str(x or "").strip()],
        }
        try:
            data = await asyncio.to_thread(self._post_json, "/v1/worker/ack", payload)
            return bool(data.get("ok", False))
        except Exception:
            return False

    def _post_json(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url=url, data=body, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("Accept", "application/json")
        if self.api_key:
            req.add_header("X-Api-Key", self.api_key)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.URLError as exc:
            # Normalize socket timeout to make errors easier to reason about.
            if isinstance(exc.reason, socket.timeout):
                raise TimeoutError("decision api timeout") from exc
            raise

        data: Optional[Dict[str, Any]] = None
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        return data or {}
