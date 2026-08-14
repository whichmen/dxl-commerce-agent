from __future__ import annotations

from dataclasses import dataclass
import re
from time import time
from typing import Any

from pydantic import BaseModel, Field, field_validator


def now_ms() -> int:
    return int(time() * 1000)


def customer_session_suffix(
    *,
    platform_nickname: str,
) -> str:
    nick = str(platform_nickname or "").strip()
    if nick:
        return nick
    raise ValueError("platform_nickname_required")


class IncomingEvent(BaseModel):
    tenant_id: str = "default"
    platform: str = "douyin"
    store_id: str = "douyin_store_default"
    store_name: str = ""
    role: str = "customer"
    customer_id: str = ""
    platform_nickname: str
    message_id: str
    message_type: str = "text"
    text: str = ""
    media_url: str = ""
    media_id: str = ""
    timestamp_ms: int = Field(default_factory=now_ms)
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("platform_nickname")
    @classmethod
    def _validate_platform_nickname(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("platform_nickname_required")
        return cleaned

    def session_key(self) -> str:
        suffix = customer_session_suffix(
            platform_nickname=self.platform_nickname,
        )
        return f"{self.platform}:{self.store_id}:{suffix}"

    def role_key(self) -> str:
        value = (self.role or "").strip().lower()
        if value in {"admin", "ops", "operator", "manager"}:
            return "admin"
        return "customer"


class DecisionRequest(BaseModel):
    event: IncomingEvent


class AgentPromptRequest(BaseModel):
    prompt: str
    session_key: str = ""
    attachments: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("prompt")
    @classmethod
    def _validate_prompt(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("prompt_required")
        return cleaned


class WorkerCaptureOcrItem(BaseModel):
    item_id: str
    bounds: str

    @field_validator("item_id")
    @classmethod
    def _validate_item_id(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("item_id_required")
        return cleaned

    @field_validator("bounds")
    @classmethod
    def _validate_bounds(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("bounds_required")
        return cleaned


class WorkerCaptureOcrBatchRequest(BaseModel):
    device_serial: str
    items: list[WorkerCaptureOcrItem] = Field(default_factory=list)

    @field_validator("device_serial")
    @classmethod
    def _validate_device_serial(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("device_serial_required")
        return cleaned


class WorkerCaptureOcrItemResult(BaseModel):
    item_id: str
    ocr_text: str = ""
    sha1: str = ""


class DecisionResponse(BaseModel):
    action: str
    reply_text: str
    reason: str
    reply_image_urls: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


@dataclass
class CategoryResult:
    category: str
    confidence: float
    reason: str


@dataclass
class DecisionInternal:
    action: str
    reply_text: str
    reason: str
    category: str
    confidence: float
    amount: float
    need_more: bool
    manual: bool
    extra: dict[str, Any]
