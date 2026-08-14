from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

from .channel_store import (
    ChannelStore,
    InboxRecord,
    OutboxLease,
    outbound_payload_hash,
)
from .connectors import (
    ChannelConnector,
    DeliveryDisposition,
    OutboundCommand,
    ReceiptDisposition,
    binding_fingerprint,
)
from .domain import ConflictError, IncomingMessage
from .runtime import AgentRuntime, redact
from .storage import canonical_hash

_HANDOFF_RE = re.compile(
    r"^\s*(?:(?:请|麻烦)(?:帮我)?|我(?:要|需要))?"
    r"(?:转|接入|联系|找)?(?:一下)?(?:人工|真人)(?:客服|服务)?[！!。.]?\s*$"
)


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    item_key: str | None
    status: str


async def _renew_lease_until_stopped(
    renew: Callable[[], None],
    stopped: asyncio.Event,
    *,
    interval_seconds: float = 20.0,
) -> None:
    while True:
        try:
            await asyncio.wait_for(stopped.wait(), timeout=interval_seconds)
            return
        except TimeoutError:
            try:
                renew()
            except ConflictError:
                return


class ConnectorRegistry:
    def __init__(self, connectors: Iterable[ChannelConnector]) -> None:
        self._connectors: dict[str, ChannelConnector] = {}
        for connector in connectors:
            if connector.connector_id in self._connectors:
                raise ValueError(f"Duplicate connector id: {connector.connector_id}")
            self._connectors[connector.connector_id] = connector

    def get(self, connector_id: str) -> ChannelConnector | None:
        return self._connectors.get(connector_id)

    def require(self, connector_id: str) -> ChannelConnector:
        connector = self.get(connector_id)
        if connector is None:
            raise LookupError(f"Unknown connector id: {connector_id}")
        return connector


class ConnectorIngestor:
    """Poll one connector and atomically persist its sanitized batch and cursor."""

    def __init__(self, *, store: ChannelStore, connector: ChannelConnector) -> None:
        self.store = store
        self.connector = connector

    async def poll_once(self, *, limit: int = 100) -> WorkerOutcome:
        expected_cursor = self.store.cursor(self.connector.connector_id)
        batch = await self.connector.poll(expected_cursor, limit)
        records: list[InboxRecord] = []
        binding = self.connector.binding
        connector_fingerprint = binding_fingerprint(binding, self.connector.capabilities)
        for event in batch.events:
            if (
                event.connector_id != binding.connector_id
                or event.tenant_id != binding.tenant_id
                or event.store_id != binding.store_id
                or event.channel != binding.channel
            ):
                raise ValueError("Normalized event escaped its trusted connector binding")
            text = redact(event.text)[:4000]
            attachments = event.attachments[:5]
            payload_fingerprint = canonical_hash(
                {
                    "connector_id": event.connector_id,
                    "external_event_id": event.external_event_id,
                    "conversation_id": event.conversation_id,
                    "customer_id": event.customer_id,
                    "message_id": event.message_id,
                    "text": text,
                    "attachments": attachments,
                    "received_at": event.received_at.isoformat(),
                }
            )
            records.append(
                InboxRecord(
                    event_key=event.event_id,
                    connector_id=event.connector_id,
                    external_event_id=event.external_event_id,
                    tenant_id=event.tenant_id,
                    channel=event.channel,
                    store_id=event.store_id,
                    conversation_id=event.conversation_id,
                    customer_id=event.customer_id,
                    message_id=event.message_id,
                    text=text,
                    attachments=attachments,
                    received_at=event.received_at.isoformat(),
                    binding_fingerprint=connector_fingerprint,
                    payload_fingerprint=payload_fingerprint,
                    supports_native_idempotency=(
                        self.connector.capabilities.supports_native_idempotency
                    ),
                    supports_receipt_lookup=(self.connector.capabilities.supports_receipt_lookup),
                )
            )
        inserted = self.store.record_poll(
            connector_id=self.connector.connector_id,
            expected_cursor=expected_cursor,
            next_cursor=batch.next_cursor,
            records=records,
        )
        return WorkerOutcome(
            item_key=self.connector.connector_id,
            status=f"ingested:{inserted}",
        )


class ConversationWorker:
    """Drive one durable inbound item through the single Agent Runtime."""

    def __init__(self, *, store: ChannelStore, runtime: AgentRuntime) -> None:
        self.store = store
        self.runtime = runtime

    async def run_once(self) -> WorkerOutcome:
        lease = self.store.claim_inbox()
        if lease is None:
            return WorkerOutcome(item_key=None, status="idle")
        stopped = asyncio.Event()
        heartbeat = asyncio.create_task(
            _renew_lease_until_stopped(
                lambda: self.store.renew_inbox(lease),
                stopped,
            )
        )
        try:
            async with self.store.session_fence(lease.session_key):
                if self.store.session_state(lease.session_key) == "human_active":
                    self.store.park_for_human(lease)
                    return WorkerOutcome(
                        item_key=lease.event_key,
                        status="parked_for_human",
                    )

                if _HANDOFF_RE.fullmatch(lease.text):
                    self.store.handoff_with_ack(
                        lease,
                        reason="customer_requested",
                        acknowledgement="已转交人工客服，后续消息将由人工接管。",
                    )
                    return WorkerOutcome(item_key=lease.event_key, status="human_handoff")

                message = IncomingMessage(
                    tenant_id=lease.tenant_id,
                    channel=lease.channel,
                    store_id=lease.store_id,
                    customer_id=lease.customer_id,
                    message_id=lease.message_id,
                    text=lease.text,
                    attachments=lease.attachments,
                    received_at=datetime.fromisoformat(lease.received_at),
                )
                decision = await self.runtime.handle_message(message)
                outbox_key = self.store.complete_with_reply(
                    lease,
                    reply=str(decision["reply"]),
                    decision=decision,
                )
                if outbox_key is None:
                    return WorkerOutcome(
                        item_key=lease.event_key,
                        status="parked_for_human",
                    )
                return WorkerOutcome(
                    item_key=lease.event_key,
                    status=str(decision["status"]),
                )
        except Exception as error:
            try:
                self.store.requeue_inbox(lease, reason=type(error).__name__)
            except ConflictError:
                return WorkerOutcome(item_key=lease.event_key, status="inbox_lease_lost")
            return WorkerOutcome(item_key=lease.event_key, status="decision_retry_scheduled")
        finally:
            stopped.set()
            await heartbeat


class DeliveryWorker:
    """Deliver one outbox command, reconciling ambiguity before any retry."""

    def __init__(self, *, store: ChannelStore, connectors: ConnectorRegistry) -> None:
        self.store = store
        self.connectors = connectors

    async def run_once(self) -> WorkerOutcome:
        lease = self.store.claim_outbox()
        if lease is None:
            return WorkerOutcome(item_key=None, status="idle")
        stopped = asyncio.Event()
        heartbeat = asyncio.create_task(
            _renew_lease_until_stopped(
                lambda: self.store.renew_outbox(lease),
                stopped,
            )
        )
        try:
            async with self.store.session_fence(lease.session_key):
                if not self.store.delivery_allowed(lease):
                    self.store.cancel_outbox(lease, reason="session_fence_changed")
                    return WorkerOutcome(
                        item_key=lease.outbox_key,
                        status="delivery_suppressed",
                    )
                connector = self.connectors.get(lease.connector_id)
                if connector is None:
                    self.store.cancel_outbox_to_handoff(
                        lease,
                        reason="connector_not_registered",
                    )
                    return WorkerOutcome(item_key=lease.outbox_key, status="human_handoff")
                if not self._binding_matches(connector, lease):
                    self.store.cancel_outbox_to_handoff(
                        lease,
                        reason="connector_binding_changed",
                    )
                    return WorkerOutcome(item_key=lease.outbox_key, status="human_handoff")
                expected_payload_hash = outbound_payload_hash(
                    outbox_key=lease.outbox_key,
                    connector_id=lease.connector_id,
                    conversation_id=lease.conversation_id,
                    body=lease.body,
                    idempotency_key=lease.idempotency_key,
                )
                if expected_payload_hash != lease.payload_hash:
                    self.store.cancel_outbox_to_handoff(
                        lease,
                        reason="outbox_payload_hash_mismatch",
                    )
                    return WorkerOutcome(item_key=lease.outbox_key, status="human_handoff")
                return await self._deliver(connector, lease)
        finally:
            stopped.set()
            await heartbeat

    async def _deliver(
        self,
        connector: ChannelConnector,
        lease: OutboxLease,
    ) -> WorkerOutcome:
        command = OutboundCommand(
            business_key=lease.outbox_key,
            conversation_id=lease.conversation_id,
            text=lease.body,
            idempotency_key=lease.idempotency_key,
        )
        try:
            attempt = await connector.send(command)
        except Exception:
            return await self._reconcile_or_handoff(
                connector,
                lease,
                provider_operation_id=None,
                reason="connector_send_exception",
            )

        if attempt.idempotency_key != lease.idempotency_key:
            self.store.fail_outbox_to_handoff(lease, reason="delivery_key_mismatch")
            return WorkerOutcome(item_key=lease.outbox_key, status="ambiguous_handoff")
        if attempt.disposition == DeliveryDisposition.CONFIRMED:
            self.store.succeed_outbox(
                lease,
                receipt=attempt.provider_operation_id or attempt.safe_code,
            )
            return WorkerOutcome(item_key=lease.outbox_key, status="delivered")
        if attempt.disposition == DeliveryDisposition.RETRYABLE:
            if not attempt.remote_attempted or connector.capabilities.supports_native_idempotency:
                self.store.requeue_outbox(
                    lease,
                    reason=attempt.safe_code,
                    retry_delay_seconds=attempt.retry_after_seconds or 0,
                )
                return WorkerOutcome(
                    item_key=lease.outbox_key,
                    status="delivery_retry_scheduled",
                )
            return await self._reconcile_or_handoff(
                connector,
                lease,
                provider_operation_id=attempt.provider_operation_id,
                reason="retryable_after_remote_attempt",
            )
        if attempt.disposition == DeliveryDisposition.PERMANENT_FAILURE:
            self.store.cancel_outbox_to_handoff(
                lease,
                reason=attempt.safe_code,
            )
            return WorkerOutcome(item_key=lease.outbox_key, status="human_handoff")
        return await self._reconcile_or_handoff(
            connector,
            lease,
            provider_operation_id=attempt.provider_operation_id,
            reason=attempt.safe_code,
        )

    @staticmethod
    def _binding_matches(connector: ChannelConnector, lease: OutboxLease) -> bool:
        binding = connector.binding
        return (
            binding.connector_id == lease.connector_id
            and binding.tenant_id == lease.tenant_id
            and binding.store_id == lease.store_id
            and binding.channel == lease.channel
            and binding_fingerprint(binding, connector.capabilities) == lease.binding_fingerprint
        )

    async def _reconcile_or_handoff(
        self,
        connector: ChannelConnector,
        lease: OutboxLease,
        *,
        provider_operation_id: str | None,
        reason: str,
    ) -> WorkerOutcome:
        if not connector.capabilities.supports_receipt_lookup:
            self.store.fail_outbox_to_handoff(lease, reason=reason)
            return WorkerOutcome(item_key=lease.outbox_key, status="ambiguous_handoff")
        try:
            receipt = await connector.lookup_receipt(
                lease.idempotency_key,
                provider_operation_id,
            )
        except Exception:
            self.store.fail_outbox_to_handoff(lease, reason="receipt_lookup_exception")
            return WorkerOutcome(item_key=lease.outbox_key, status="ambiguous_handoff")

        if receipt.idempotency_key != lease.idempotency_key:
            self.store.fail_outbox_to_handoff(lease, reason="receipt_key_mismatch")
            return WorkerOutcome(item_key=lease.outbox_key, status="ambiguous_handoff")
        if receipt.disposition == ReceiptDisposition.CONFIRMED:
            self.store.succeed_outbox(
                lease,
                receipt=receipt.provider_operation_id or receipt.safe_code,
            )
            return WorkerOutcome(item_key=lease.outbox_key, status="reconciled_delivered")
        if (
            receipt.disposition == ReceiptDisposition.NOT_FOUND_SAFE_TO_RETRY
            and connector.capabilities.supports_native_idempotency
        ):
            self.store.requeue_outbox(lease, reason=receipt.safe_code)
            return WorkerOutcome(item_key=lease.outbox_key, status="delivery_retry_scheduled")
        self.store.fail_outbox_to_handoff(lease, reason=receipt.safe_code)
        return WorkerOutcome(item_key=lease.outbox_key, status="ambiguous_handoff")


class ConnectorSupervisor:
    """Return safe health codes suitable for a watchdog or readiness endpoint."""

    def __init__(self, connectors: ConnectorRegistry) -> None:
        self.connectors = connectors

    async def check(self, connector_ids: Iterable[str]) -> dict[str, dict[str, object]]:
        snapshot: dict[str, dict[str, object]] = {}
        for connector_id in connector_ids:
            connector = self.connectors.get(connector_id)
            if connector is None:
                snapshot[connector_id] = {
                    "online": False,
                    "ready": False,
                    "safe_code": "connector_not_registered",
                }
                continue
            health = await connector.health()
            snapshot[connector_id] = {
                "online": health.online,
                "ready": health.ready,
                "safe_code": health.safe_code,
            }
        return snapshot
