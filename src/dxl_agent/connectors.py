"""Clean-room synthetic channel connectors used by the public demo.

The classes in this module do not contain real platform URLs, selectors, wire
formats, credentials, or automation code.  They model the reliability contract
between a channel adapter and a worker using deterministic in-memory state.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from .domain import IncomingMessage, scoped_key

_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_TOKEN_RE = re.compile(r"(?i)(bearer\s+|api[_-]?key[=:]\s*)[A-Za-z0-9._-]{8,}")


def _redact(text: str) -> str:
    text = _PHONE_RE.sub("[REDACTED_PHONE]", text)
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    return _TOKEN_RE.sub("[REDACTED_SECRET]", text)


def _require_text(value: str, field_name: str, *, max_length: int = 256) -> None:
    if not value or len(value) > max_length:
        raise ConnectorProtocolError(f"{field_name} must contain 1-{max_length} characters")


def _payload_hash(message: OutboundCommand) -> str:
    payload = json.dumps(
        {
            "business_key": message.business_key,
            "conversation_id": message.conversation_id,
            "text": message.text,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class ConnectorProtocolError(ValueError):
    """The caller violated the synthetic connector contract."""


class DeliveryDisposition(StrEnum):
    CONFIRMED = "confirmed"
    RETRYABLE = "retryable"
    PERMANENT_FAILURE = "permanent_failure"
    UNKNOWN = "unknown"


class ReceiptDisposition(StrEnum):
    CONFIRMED = "confirmed"
    NOT_FOUND_SAFE_TO_RETRY = "not_found_safe_to_retry"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ConnectorBinding:
    """Trusted deployment configuration for one synthetic channel account."""

    connector_id: str
    tenant_id: str
    store_id: str
    channel: str

    def __post_init__(self) -> None:
        _require_text(self.connector_id, "connector_id", max_length=64)
        _require_text(self.tenant_id, "tenant_id", max_length=64)
        _require_text(self.store_id, "store_id", max_length=64)
        _require_text(self.channel, "channel", max_length=64)


@dataclass(frozen=True, slots=True)
class ConnectorCapabilities:
    inbound_text: bool
    inbound_attachment_refs: bool
    outbound_text: bool
    max_outbound_chars: int
    supports_native_idempotency: bool
    supports_receipt_lookup: bool


def binding_fingerprint(
    binding: ConnectorBinding,
    capabilities: ConnectorCapabilities,
) -> str:
    """Snapshot routing identity and delivery semantics for a queued command."""

    return scoped_key(
        "connector_binding",
        binding.connector_id,
        binding.tenant_id,
        binding.store_id,
        binding.channel,
        str(capabilities.max_outbound_chars),
        str(capabilities.supports_native_idempotency),
        str(capabilities.supports_receipt_lookup),
    )


@dataclass(frozen=True, slots=True)
class SyntheticInboundEvent:
    """Invented input shape; metadata is explicitly untrusted and never persisted."""

    external_message_id: str
    conversation_id: str
    customer_ref: str
    text: str
    attachment_refs: tuple[str, ...] = ()
    source_sequence: int | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    untrusted_metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.external_message_id, "external_message_id", max_length=128)
        _require_text(self.conversation_id, "conversation_id", max_length=128)
        _require_text(self.customer_ref, "customer_ref", max_length=128)
        _require_text(self.text, "text", max_length=4000)
        if len(self.attachment_refs) > 5:
            raise ConnectorProtocolError("attachment_refs supports at most 5 items")
        if self.source_sequence is not None and self.source_sequence < 0:
            raise ConnectorProtocolError("source_sequence cannot be negative")


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    event_id: str
    connector_id: str
    tenant_id: str
    store_id: str
    channel: str
    conversation_id: str
    customer_ref: str
    external_message_id: str
    text: str
    attachment_refs: tuple[str, ...]
    source_sequence: int | None
    occurred_at: datetime
    raw_fingerprint: str

    @property
    def external_event_id(self) -> str:
        """Synthetic source event identifier expected by worker adapters."""

        return self.external_message_id

    @property
    def customer_id(self) -> str:
        return self.customer_ref

    @property
    def message_id(self) -> str:
        return self.external_message_id

    @property
    def attachments(self) -> tuple[str, ...]:
        return self.attachment_refs

    @property
    def received_at(self) -> datetime:
        return self.occurred_at

    @property
    def session_key(self) -> str:
        return scoped_key(
            "session",
            self.tenant_id,
            self.channel,
            self.store_id,
            self.customer_ref,
        )

    def to_incoming_message(self) -> IncomingMessage:
        return IncomingMessage(
            tenant_id=self.tenant_id,
            channel=self.channel,
            store_id=self.store_id,
            customer_id=self.customer_ref,
            message_id=self.external_message_id,
            text=self.text,
            attachments=self.attachment_refs,
            received_at=self.occurred_at,
        )


@dataclass(frozen=True, slots=True)
class PollBatch:
    events: tuple[NormalizedEvent, ...]
    next_cursor: str


@dataclass(frozen=True, slots=True)
class OutboundCommand:
    """One semantic send operation.

    ``business_key`` remains stable even if a caller mistakenly generates a new
    transport idempotency key.  This lets the mobile connector latch an unknown
    result and refuse a blind retry of the same semantic effect.
    """

    business_key: str
    conversation_id: str
    text: str
    idempotency_key: str

    def __post_init__(self) -> None:
        _require_text(self.business_key, "business_key", max_length=256)
        _require_text(self.conversation_id, "conversation_id", max_length=128)
        _require_text(self.text, "text", max_length=4000)
        _require_text(self.idempotency_key, "idempotency_key", max_length=128)
        if len(self.idempotency_key) < 8:
            raise ConnectorProtocolError("idempotency_key must contain at least 8 characters")


@dataclass(frozen=True, slots=True)
class DeliveryAttempt:
    disposition: DeliveryDisposition
    idempotency_key: str
    provider_operation_id: str | None
    safe_code: str
    remote_attempted: bool
    deduplicated: bool = False
    retry_after_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ReceiptCheck:
    disposition: ReceiptDisposition
    idempotency_key: str
    provider_operation_id: str | None
    safe_code: str


@dataclass(frozen=True, slots=True)
class ConnectorHealth:
    online: bool
    ready: bool
    safe_code: str


class ChannelConnector(Protocol):
    binding: ConnectorBinding
    capabilities: ConnectorCapabilities

    @property
    def connector_id(self) -> str: ...

    async def poll(self, cursor: str | None = None, limit: int = 100) -> PollBatch: ...

    async def send(self, message: OutboundCommand) -> DeliveryAttempt: ...

    async def lookup_receipt(
        self,
        idempotency_key: str,
        provider_operation_id: str | None = None,
    ) -> ReceiptCheck: ...

    async def health(self) -> ConnectorHealth: ...


@dataclass(frozen=True, slots=True)
class _SentRecord:
    business_key: str
    idempotency_key: str
    payload_hash: str
    operation_id: str


class _SyntheticConnectorBase:
    def __init__(
        self,
        binding: ConnectorBinding,
        capabilities: ConnectorCapabilities,
        events: tuple[SyntheticInboundEvent, ...] = (),
    ) -> None:
        self.binding = binding
        self.capabilities = capabilities
        self._events = list(events)
        self._remote_attempt_count = 0

    @property
    def connector_id(self) -> str:
        return self.binding.connector_id

    @property
    def remote_attempt_count(self) -> int:
        return self._remote_attempt_count

    def append_synthetic_event(self, event: SyntheticInboundEvent) -> None:
        self._events.append(event)

    async def poll(self, cursor: str | None = None, limit: int = 100) -> PollBatch:
        if limit < 1 or limit > 100:
            raise ConnectorProtocolError("limit must be between 1 and 100")
        offset = self._parse_cursor(cursor)
        selected = self._events[offset : offset + limit]
        return PollBatch(
            events=tuple(self._normalize(event) for event in selected),
            next_cursor=str(offset + len(selected)),
        )

    def _parse_cursor(self, cursor: str | None) -> int:
        if cursor is None:
            return 0
        try:
            offset = int(cursor)
        except ValueError as error:
            raise ConnectorProtocolError("cursor must be a non-negative integer") from error
        if offset < 0 or offset > len(self._events):
            raise ConnectorProtocolError("cursor is outside the synthetic event stream")
        return offset

    def _normalize(self, event: SyntheticInboundEvent) -> NormalizedEvent:
        raw_projection = {
            "external_message_id": event.external_message_id,
            "conversation_id": event.conversation_id,
            "customer_ref": event.customer_ref,
            "text": event.text,
            "attachment_refs": event.attachment_refs,
            "source_sequence": event.source_sequence,
            "occurred_at": event.occurred_at.isoformat(),
        }
        raw_fingerprint = hashlib.sha256(
            json.dumps(
                raw_projection,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return NormalizedEvent(
            event_id=scoped_key(
                "connector_event",
                self.binding.connector_id,
                event.external_message_id,
            ),
            connector_id=self.binding.connector_id,
            tenant_id=self.binding.tenant_id,
            store_id=self.binding.store_id,
            channel=self.binding.channel,
            conversation_id=event.conversation_id,
            customer_ref=event.customer_ref,
            external_message_id=event.external_message_id,
            text=_redact(event.text),
            attachment_refs=event.attachment_refs,
            source_sequence=event.source_sequence,
            occurred_at=event.occurred_at,
            raw_fingerprint=raw_fingerprint,
        )

    def _validate_outbound(self, message: OutboundCommand) -> None:
        if not self.capabilities.outbound_text:
            raise ConnectorProtocolError("connector does not support outbound text")
        if len(message.text) > self.capabilities.max_outbound_chars:
            raise ConnectorProtocolError(
                f"outbound text exceeds {self.capabilities.max_outbound_chars} characters"
            )


class BrowserConnector(_SyntheticConnectorBase):
    """Synthetic browser-shaped connector with native idempotency and receipts."""

    def __init__(
        self,
        binding: ConnectorBinding,
        events: tuple[SyntheticInboundEvent, ...] = (),
    ) -> None:
        super().__init__(
            binding,
            ConnectorCapabilities(
                inbound_text=True,
                inbound_attachment_refs=True,
                outbound_text=True,
                max_outbound_chars=2000,
                supports_native_idempotency=True,
                supports_receipt_lookup=True,
            ),
            events,
        )
        self._sent_by_idempotency: dict[str, _SentRecord] = {}
        self._sent_by_business_key: dict[str, _SentRecord] = {}
        self._timeout_after_commit_once = False

    def simulate_timeout_after_commit_once(self) -> None:
        self._timeout_after_commit_once = True

    async def send(self, message: OutboundCommand) -> DeliveryAttempt:
        self._validate_outbound(message)
        fingerprint = _payload_hash(message)
        existing = self._sent_by_idempotency.get(message.idempotency_key)
        if existing is not None:
            self._ensure_same_payload(existing, fingerprint)
            return self._confirmed(existing, deduplicated=True, remote_attempted=False)

        existing_business = self._sent_by_business_key.get(message.business_key)
        if existing_business is not None:
            self._ensure_same_payload(existing_business, fingerprint)
            return self._confirmed(
                existing_business,
                deduplicated=True,
                remote_attempted=False,
            )

        self._remote_attempt_count += 1
        record = _SentRecord(
            business_key=message.business_key,
            idempotency_key=message.idempotency_key,
            payload_hash=fingerprint,
            operation_id=f"SYN-BROWSER-OP-{self._remote_attempt_count:04d}",
        )
        self._sent_by_idempotency[message.idempotency_key] = record
        self._sent_by_business_key[message.business_key] = record
        if self._timeout_after_commit_once:
            self._timeout_after_commit_once = False
            return DeliveryAttempt(
                disposition=DeliveryDisposition.UNKNOWN,
                idempotency_key=message.idempotency_key,
                provider_operation_id=None,
                safe_code="synthetic_timeout_after_commit",
                remote_attempted=True,
            )
        return self._confirmed(record, deduplicated=False, remote_attempted=True)

    async def lookup_receipt(
        self,
        idempotency_key: str,
        provider_operation_id: str | None = None,
    ) -> ReceiptCheck:
        record = self._sent_by_idempotency.get(idempotency_key)
        if record is None:
            return ReceiptCheck(
                disposition=ReceiptDisposition.NOT_FOUND_SAFE_TO_RETRY,
                idempotency_key=idempotency_key,
                provider_operation_id=None,
                safe_code="synthetic_receipt_not_found",
            )
        if provider_operation_id is not None and provider_operation_id != record.operation_id:
            return ReceiptCheck(
                disposition=ReceiptDisposition.UNKNOWN,
                idempotency_key=idempotency_key,
                provider_operation_id=provider_operation_id,
                safe_code="synthetic_operation_id_mismatch",
            )
        return ReceiptCheck(
            disposition=ReceiptDisposition.CONFIRMED,
            idempotency_key=idempotency_key,
            provider_operation_id=record.operation_id,
            safe_code="synthetic_delivery_confirmed",
        )

    async def health(self) -> ConnectorHealth:
        return ConnectorHealth(online=True, ready=True, safe_code="synthetic_browser_ready")

    @staticmethod
    def _ensure_same_payload(record: _SentRecord, fingerprint: str) -> None:
        if record.payload_hash != fingerprint:
            raise ConnectorProtocolError(
                "business or idempotency key was reused with a different payload"
            )

    @staticmethod
    def _confirmed(
        record: _SentRecord,
        *,
        deduplicated: bool,
        remote_attempted: bool,
    ) -> DeliveryAttempt:
        return DeliveryAttempt(
            disposition=DeliveryDisposition.CONFIRMED,
            idempotency_key=record.idempotency_key,
            provider_operation_id=record.operation_id,
            safe_code="synthetic_delivery_confirmed",
            remote_attempted=remote_attempted,
            deduplicated=deduplicated,
        )


class MobileConnector(_SyntheticConnectorBase):
    """Synthetic mobile-shaped connector with conservative unknown-result handling."""

    def __init__(
        self,
        binding: ConnectorBinding,
        events: tuple[SyntheticInboundEvent, ...] = (),
    ) -> None:
        super().__init__(
            binding,
            ConnectorCapabilities(
                inbound_text=True,
                inbound_attachment_refs=True,
                outbound_text=True,
                max_outbound_chars=1000,
                supports_native_idempotency=False,
                supports_receipt_lookup=False,
            ),
            events,
        )
        self._online = True
        self._foreground_available = True
        self._unknown_on_next_send = False
        self._confirmed_by_idempotency: dict[str, _SentRecord] = {}
        self._confirmed_by_business_key: dict[str, _SentRecord] = {}
        self._unknown_by_business_key: dict[str, str] = {}
        self._unknown_idempotency_keys: set[str] = set()

    def set_online(self, online: bool) -> None:
        self._online = online

    def set_foreground_available(self, available: bool) -> None:
        self._foreground_available = available

    def simulate_unknown_result_once(self) -> None:
        self._unknown_on_next_send = True

    async def send(self, message: OutboundCommand) -> DeliveryAttempt:
        self._validate_outbound(message)
        fingerprint = _payload_hash(message)

        unknown_key = self._unknown_by_business_key.get(message.business_key)
        if unknown_key is not None:
            return DeliveryAttempt(
                disposition=DeliveryDisposition.UNKNOWN,
                idempotency_key=unknown_key,
                provider_operation_id=None,
                safe_code="prior_synthetic_result_unknown_no_blind_retry",
                remote_attempted=False,
                deduplicated=True,
            )

        existing = self._confirmed_by_idempotency.get(message.idempotency_key)
        if existing is not None:
            BrowserConnector._ensure_same_payload(existing, fingerprint)
            return BrowserConnector._confirmed(
                existing,
                deduplicated=True,
                remote_attempted=False,
            )
        existing_business = self._confirmed_by_business_key.get(message.business_key)
        if existing_business is not None:
            BrowserConnector._ensure_same_payload(existing_business, fingerprint)
            return BrowserConnector._confirmed(
                existing_business,
                deduplicated=True,
                remote_attempted=False,
            )

        if not self._online:
            return DeliveryAttempt(
                disposition=DeliveryDisposition.RETRYABLE,
                idempotency_key=message.idempotency_key,
                provider_operation_id=None,
                safe_code="synthetic_device_offline",
                remote_attempted=False,
                retry_after_seconds=5.0,
            )
        if not self._foreground_available:
            return DeliveryAttempt(
                disposition=DeliveryDisposition.RETRYABLE,
                idempotency_key=message.idempotency_key,
                provider_operation_id=None,
                safe_code="synthetic_foreground_lease_lost",
                remote_attempted=False,
                retry_after_seconds=2.0,
            )

        self._remote_attempt_count += 1
        if self._unknown_on_next_send:
            self._unknown_on_next_send = False
            self._unknown_by_business_key[message.business_key] = message.idempotency_key
            self._unknown_idempotency_keys.add(message.idempotency_key)
            return DeliveryAttempt(
                disposition=DeliveryDisposition.UNKNOWN,
                idempotency_key=message.idempotency_key,
                provider_operation_id=None,
                safe_code="synthetic_mobile_result_unknown",
                remote_attempted=True,
            )

        record = _SentRecord(
            business_key=message.business_key,
            idempotency_key=message.idempotency_key,
            payload_hash=fingerprint,
            operation_id=f"SYN-MOBILE-OP-{self._remote_attempt_count:04d}",
        )
        self._confirmed_by_idempotency[message.idempotency_key] = record
        self._confirmed_by_business_key[message.business_key] = record
        return BrowserConnector._confirmed(
            record,
            deduplicated=False,
            remote_attempted=True,
        )

    async def lookup_receipt(
        self,
        idempotency_key: str,
        provider_operation_id: str | None = None,
    ) -> ReceiptCheck:
        record = self._confirmed_by_idempotency.get(idempotency_key)
        if record is not None:
            if provider_operation_id is not None and provider_operation_id != record.operation_id:
                return ReceiptCheck(
                    disposition=ReceiptDisposition.UNKNOWN,
                    idempotency_key=idempotency_key,
                    provider_operation_id=provider_operation_id,
                    safe_code="synthetic_operation_id_mismatch",
                )
            return ReceiptCheck(
                disposition=ReceiptDisposition.CONFIRMED,
                idempotency_key=idempotency_key,
                provider_operation_id=record.operation_id,
                safe_code="synthetic_local_confirmation",
            )
        if idempotency_key in self._unknown_idempotency_keys:
            return ReceiptCheck(
                disposition=ReceiptDisposition.UNKNOWN,
                idempotency_key=idempotency_key,
                provider_operation_id=None,
                safe_code="synthetic_mobile_receipt_unavailable",
            )
        return ReceiptCheck(
            disposition=ReceiptDisposition.UNKNOWN,
            idempotency_key=idempotency_key,
            provider_operation_id=None,
            safe_code="synthetic_mobile_has_no_receipt_lookup",
        )

    async def health(self) -> ConnectorHealth:
        if not self._online:
            return ConnectorHealth(
                online=False,
                ready=False,
                safe_code="synthetic_device_offline",
            )
        if not self._foreground_available:
            return ConnectorHealth(
                online=True,
                ready=False,
                safe_code="synthetic_foreground_lease_lost",
            )
        return ConnectorHealth(online=True, ready=True, safe_code="synthetic_mobile_ready")


# Compatibility aliases keep the public vocabulary readable for callers that
# naturally refer to outbound messages and delivery receipts.
OutboundMessage = OutboundCommand
DeliveryReceipt = ReceiptCheck
