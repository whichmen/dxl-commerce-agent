"""Data models for worker <-> decision-service communication."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from time import time
from typing import Any, Dict, List, Optional


def now_ms() -> int:
    """Return current unix timestamp in milliseconds."""
    return int(time() * 1000)


@dataclass
class IncomingMessage:
    """Normalized incoming message event from any platform worker."""

    tenant_id: str
    platform: str
    store_id: str
    store_name: str
    customer_id: str
    platform_nickname: str
    message_id: str
    message_type: str
    text: str = ""
    media_url: str = ""
    timestamp_ms: int = field(default_factory=now_ms)
    raw: Dict[str, Any] = field(default_factory=dict)
    role: str = "customer"

    def session_key(self) -> str:
        nickname = str(self.platform_nickname or "").strip()
        if not nickname:
            raise ValueError("platform_nickname_required")
        return f"{self.platform}:{self.store_id}:{nickname}"

    def as_payload(self) -> Dict[str, Any]:
        data = asdict(self)
        data["session_key"] = self.session_key()
        return data


@dataclass
class DecisionResult:
    """Decision response returned by Ubuntu decision service."""

    action: str
    reply_text: str
    reason: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)
    reply_image_urls: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "DecisionResult":
        data = data or {}
        extra = data.get("extra")
        extra_dict = extra if isinstance(extra, dict) else {}
        urls: List[str] = []
        for source in (
            data.get("reply_image_urls"),
            extra_dict.get("reply_image_urls"),
        ):
            if isinstance(source, list):
                for item in source:
                    val = str(item or "").strip()
                    if val and val not in urls:
                        urls.append(val)
            elif isinstance(source, str):
                val = source.strip()
                if val and val not in urls:
                    urls.append(val)
        for single in (
            data.get("reply_image_url"),
            extra_dict.get("reply_image_url"),
            extra_dict.get("image_url"),
        ):
            val = str(single or "").strip()
            if val and val not in urls:
                urls.append(val)
        return cls(
            action=str(data.get("action") or "skip"),
            reply_text=str(data.get("reply_text") or ""),
            reason=str(data.get("reason") or ""),
            extra=extra_dict,
            reply_image_urls=urls,
        )
